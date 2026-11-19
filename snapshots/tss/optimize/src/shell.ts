// MCP server 2: `shell` (SPEC section 3).
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { commandRejection } from "./allow.js";
import { Limiter } from "./limit.js";
import { LazyValidator } from "./validator.js";
import { errorResult, lastChars, okResult, ToolFail, type ToolResult } from "./result.js";

const TIMEOUT_MS = 30_000;
const KEEP_CHARS = 8_000;
// Bytes kept per stream while a command runs; far more than KEEP_CHARS characters can need.
const KEEP_BYTES = 256 * 1024;

const TOOLS = [
  {
    name: "run_tests",
    description:
      "Run the task's test command in the workspace. Returns exit_code, passed (exit_code == 0), the last 8000 " +
      "characters of combined stdout and stderr, and duration_ms. 30 s timeout.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "run_command",
    description:
      "Run one allowlisted command in the workspace without a shell: ls, cat, head, tail, wc, diff, python3, or " +
      "git status|diff|log|show. Returns exit_code, stdout, stderr (last 8000 characters each) and duration_ms. " +
      "30 s timeout.",
    inputSchema: {
      type: "object",
      properties: {
        argv: {
          type: "array",
          items: { type: "string" },
          minItems: 1,
          description: "Program and arguments, e.g. [\"git\", \"diff\"].",
        },
      },
      required: ["argv"],
    },
  },
];

/** One shell server's settings. `procs`, when set, caps the child processes running at once across every server
 * that shares it (used by `batch`; the OS process limit is per user). */
export interface ShellOptions {
  procs?: Limiter;
}

interface Shell {
  root: string;
  taskFile: string;
  procs?: Limiter;
}

// Spawn failures that mean "the system is busy right now" (process or file table full): wait and try again.
const SPAWN_BUSY = new Set(["EAGAIN", "EMFILE", "ENFILE", "ENOMEM"]);
const SPAWN_RETRY_MS = 50;
const SPAWN_GIVE_UP_MS = 60_000;

/** Keeps the tail of a byte stream within a bounded buffer. */
class Tail {
  chunks: Buffer[] = [];
  size = 0;
  push(b: Buffer): void {
    this.chunks.push(b);
    this.size += b.length;
    while (this.chunks.length > 1 && this.size - this.chunks[0].length >= KEEP_BYTES) {
      this.size -= this.chunks.shift()!.length;
    }
  }
  text(): string {
    return lastChars(Buffer.concat(this.chunks).toString("utf8"), KEEP_CHARS);
  }
}

interface ProcResult {
  exitCode: number;
  stdout: Tail;
  stderr: Tail;
  combined: Tail;
  ms: number;
}

function childEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const k of ["PATH", "HOME", "LANG"]) {
    const v = process.env[k];
    if (v !== undefined) env[k] = v;
  }
  return env;
}

async function runProcess(sh: Shell, argv: string[]): Promise<ProcResult> {
  const release = sh.procs ? await sh.procs.acquire() : undefined;
  try {
    const t0 = performance.now();
    for (;;) {
      try {
        return await spawnOnce(sh.root, argv);
      } catch (e) {
        const code = (e as NodeJS.ErrnoException).code;
        if (code === undefined || !SPAWN_BUSY.has(code) || performance.now() - t0 > SPAWN_GIVE_UP_MS) {
          throw new ToolFail(
            code === "ENOENT" ? `command not found: ${argv[0]}` : `cannot run ${argv[0]}: ${(e as Error).message}`,
          );
        }
        await new Promise((r) => setTimeout(r, SPAWN_RETRY_MS));
      }
    }
  } finally {
    release?.();
  }
}

/** Runs argv once. Rejects with the spawn error (errno code kept) if the process cannot be started. */
function spawnOnce(cwd: string, argv: string[]): Promise<ProcResult> {
  return new Promise((resolve, reject) => {
    const t0 = performance.now();
    const stdout = new Tail();
    const stderr = new Tail();
    const combined = new Tail();
    let timedOut = false;
    let settled = false;
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(argv[0], argv.slice(1), {
        cwd,
        env: childEnv(),
        stdio: ["ignore", "pipe", "pipe"],
        detached: true, // own process group, so a timeout kills grandchildren too
        shell: false,
      });
    } catch (e) {
      reject(e);
      return;
    }
    const killGroup = () => {
      try {
        if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
      } catch {
        try {
          child.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      }
    };
    const finish = (code: number | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (grace) clearTimeout(grace);
      resolve({
        exitCode: timedOut || code === null ? -1 : code,
        stdout,
        stderr,
        combined,
        ms: Math.round(performance.now() - t0),
      });
    };
    let grace: NodeJS.Timeout | undefined;
    const timer = setTimeout(() => {
      timedOut = true;
      killGroup();
      // If something still holds the pipes open, stop waiting shortly after the kill.
      grace = setTimeout(() => {
        child.stdout?.destroy();
        child.stderr?.destroy();
        finish(null);
      }, 2000);
    }, TIMEOUT_MS);
    child.stdout!.on("data", (b: Buffer) => {
      stdout.push(b);
      combined.push(b);
    });
    child.stderr!.on("data", (b: Buffer) => {
      stderr.push(b);
      combined.push(b);
    });
    child.on("error", (e: NodeJS.ErrnoException) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (grace) clearTimeout(grace);
      child.stdout?.destroy();
      child.stderr?.destroy();
      reject(e);
    });
    child.on("close", (code) => finish(code));
  });
}

function readTestCommand(sh: Shell): string[] {
  let raw: string;
  try {
    raw = fs.readFileSync(sh.taskFile, "utf8");
  } catch {
    throw new ToolFail(`cannot read task file: ${sh.taskFile}`);
  }
  let cmd: unknown;
  try {
    cmd = (JSON.parse(raw) as { test_command?: unknown }).test_command;
  } catch {
    throw new ToolFail("task file is not valid JSON");
  }
  if (!Array.isArray(cmd) || cmd.length < 1 || !cmd.every((a) => typeof a === "string")) {
    throw new ToolFail("task file test_command must be a non-empty array of strings");
  }
  return cmd as string[];
}

async function runTests(sh: Shell): Promise<unknown> {
  const r = await runProcess(sh, readTestCommand(sh));
  return { exit_code: r.exitCode, passed: r.exitCode === 0, output: r.combined.text(), duration_ms: r.ms };
}

async function runCommand(sh: Shell, args: Record<string, unknown>): Promise<unknown> {
  const reason = commandRejection(args.argv);
  if (reason) throw new ToolFail(reason);
  const r = await runProcess(sh, args.argv as string[]);
  return { exit_code: r.exitCode, stdout: r.stdout.text(), stderr: r.stderr.text(), duration_ms: r.ms };
}

async function call(sh: Shell, name: string, args: Record<string, unknown>): Promise<ToolResult> {
  try {
    if (name === "run_tests") return okResult(await runTests(sh));
    if (name === "run_command") return okResult(await runCommand(sh, args));
    return errorResult(`unknown tool: ${name}`);
  } catch (e) {
    return errorResult(e instanceof ToolFail ? e.message : ((e as Error)?.message ?? String(e)));
  }
}

/** A shell server (SDK low-level `Server`) for one workspace and task, not yet connected to a transport. Throws if
 * the workspace does not exist. */
export function createShellServer(workspace: string, task: string, opts: ShellOptions = {}): Server {
  const sh: Shell = { root: fs.realpathSync(path.resolve(workspace)), taskFile: path.resolve(task), procs: opts.procs };
  const server = new Server(
    { name: "tinycoder-shell", version: "1.0.0" },
    { capabilities: { tools: {} }, jsonSchemaValidator: new LazyValidator() },
  );
  server.setRequestHandler(ListToolsRequestSchema, () => ({ tools: TOOLS }));
  server.setRequestHandler(CallToolRequestSchema, (req) =>
    call(sh, req.params.name, (req.params.arguments ?? {}) as Record<string, unknown>),
  );
  return server;
}

export async function serveShell(workspace: string, task: string): Promise<void> {
  await createShellServer(workspace, task).connect(new StdioServerTransport());
}
