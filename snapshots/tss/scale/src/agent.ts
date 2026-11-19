// The agent loop (SPEC section 4). `run` drives one agent with stdio server processes; `batch` (src/batch.ts) drives
// many through `runAgent` with in-process servers and one shared Anthropic client.
import * as fs from "node:fs";
import * as path from "node:path";
import type Anthropic from "@anthropic-ai/sdk";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { commandRejection } from "./allow.js";
import { LazyValidator } from "./validator.js";

const MODEL = "claude-sonnet-5-5";
const API_VERSION = "2026-10-21";
const MAX_TOKENS = 16000;
const MAX_STEPS = 20;
const MAX_WRITE_BYTES = 100_000;
const RETRY_STATUS = new Set([429, 500, 502, 503, 529]);
const RETRY_DELAYS_MS = [1000, 2000, 4000];
const TOOL_TIMEOUT_MS = 10 * 60_000;

const ROOT = path.resolve(__dirname, "..");
const LAUNCHER = path.join(ROOT, "bin", "tinycoder");

export interface RunOptions {
  task: string;
  workspace: string;
  runs: string;
}

type Status = "done" | "step_limit" | "error";

export type Sdk = typeof import("@anthropic-ai/sdk");

/** The final object of SPEC section 4.5. */
export interface Final {
  status: Status;
  reason: string | null;
  steps: number;
  tool_calls: number;
  blocked_calls: number;
  input_tokens: number;
  output_tokens: number;
  wall_ms: number;
  llm_ms: number;
  tool_ms: number;
  final_text: string;
}

/** What differs between `run` and one agent of `batch`. */
export interface AgentHost {
  runId: string;
  /** performance.now() value that wall_ms is measured from. */
  t0: number;
  /** Transports to this agent's own `files` and `shell` servers (`taskJson` is the task's task.json). Called once. */
  transports(workspace: string, taskJson: string): { files: Transport; shell: Transport };
  /** The Anthropic SDK and a client configured as in SPEC section 4.1 (`batch` shares one between agents). */
  model(): Promise<{ sdk: Sdk; client: Anthropic }>;
  /** Called once with the final object, after its runs-file line and before the servers are closed. */
  report(final: Final): void;
}

export function exitCode(status: Status): number {
  return status === "done" ? 0 : status === "step_limit" ? 2 : 1;
}

interface Block {
  type: string;
  [key: string]: unknown;
}

interface ToolDef {
  name: string;
  description?: string;
  input_schema: unknown;
}

class Runs {
  constructor(private file: string) {}
  write(obj: Record<string, unknown>): void {
    try {
      fs.appendFileSync(this.file, JSON.stringify(obj) + "\n");
    } catch (e) {
      process.stderr.write(`tinycoder: cannot write runs file: ${(e as Error).message}\n`);
    }
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Pre-call hook. Returns a reason when the call must be blocked. */
function hookReason(name: string, input: Record<string, unknown>, known: Set<string>): string | null {
  if (!known.has(name)) return `unknown tool: ${name}`;
  const p = input.path;
  if (typeof p === "string") {
    if (path.isAbsolute(p) || p.startsWith("/") || p.startsWith("\\") || /^[A-Za-z]:/.test(p)) {
      return `absolute path not allowed: ${p}`;
    }
    if (p.split(/[\\/]/).includes("..")) return `path contains '..': ${p}`;
  }
  if (name === "write_file" && typeof input.content === "string") {
    const n = Buffer.byteLength(input.content, "utf8");
    if (n > MAX_WRITE_BYTES) return `write_file content is ${n} bytes, over the ${MAX_WRITE_BYTES} byte limit`;
  }
  if (name === "run_command") {
    const r = commandRejection(input.argv);
    if (r) return r;
  }
  return null;
}

function textOf(content: Block[] | undefined): string {
  if (!content) return "";
  return content
    .filter((b) => b.type === "text")
    .map((b) => String(b.text ?? ""))
    .join("\n");
}

function isRetryable(e: unknown, sdk: typeof import("@anthropic-ai/sdk")): boolean {
  if (e instanceof sdk.APIConnectionError) return true; // includes timeouts
  if (e instanceof sdk.APIError) return e.status !== undefined && RETRY_STATUS.has(e.status);
  return false;
}

function errText(e: unknown): string {
  if (e && typeof e === "object" && "status" in e && (e as { status?: unknown }).status !== undefined) {
    return `api error ${(e as { status: number }).status}: ${(e as unknown as Error).message}`;
  }
  return (e as Error)?.message ?? String(e);
}

function connect(transport: Transport): { client: Client; ready: Promise<void> } {
  const client = new Client({ name: "tinycoder", version: "1.0.0" }, { jsonSchemaValidator: new LazyValidator() });
  const ready = client.connect(transport);
  // Awaited on the normal path; this only keeps an early exit (setup error) from becoming an unhandled rejection.
  ready.catch(() => {});
  return { client, ready };
}

/** A client configured as SPEC section 4.1 says. SDK retries are off: `runAgent` retries itself. */
export function makeClient(sdk: Sdk): Anthropic {
  return new sdk.default({
    apiKey: process.env.ANTHROPIC_API_KEY ?? "",
    baseURL: process.env.ANTHROPIC_BASE_URL || "https://api.anthropic.com",
    maxRetries: 0,
    defaultHeaders: { "anthropic-version": API_VERSION },
  });
}

/** `tinycoder run`: one agent, each server a child process of this program on stdio. Returns the exit code. */
export async function run(opts: RunOptions): Promise<number> {
  const host: AgentHost = {
    runId: process.env.TINYCODER_RUN_ID || String(Date.now()),
    t0: 0, // wall_ms is measured from process start
    transports(workspace, taskJson) {
      const env: Record<string, string> = {};
      for (const [k, v] of Object.entries(process.env)) if (v !== undefined) env[k] = v;
      const stdio = (args: string[]) =>
        new StdioClientTransport({
          command: process.execPath,
          // Same V8 flags as this process (from the launcher's shebang), so the servers can use their code caches.
          args: [...process.execArgv, LAUNCHER, ...args],
          env,
          stderr: "inherit",
        });
      return {
        files: stdio(["serve", "files", "--workspace", workspace]),
        shell: stdio(["serve", "shell", "--workspace", workspace, "--task", taskJson]),
      };
    },
    // While the servers boot (~85 ms, in parallel processes), do this process's remaining one-time work so it is off
    // the critical path: load the Anthropic SDK, and make Node load its lazily loaded fetch implementation (undici,
    // ~40 ms on first use). Nothing is sent anywhere.
    model: () =>
      Promise.resolve().then(() => {
        const sdk = require("@anthropic-ai/sdk") as Sdk;
        new Response(new Request("http://localhost/", { method: "POST", headers: new Headers({ a: "b" }), body: "{}" }).body);
        return { sdk, client: makeClient(sdk) };
      }),
    report: (final) => fs.writeSync(1, JSON.stringify(final) + "\n"),
  };
  return exitCode((await runAgent(opts, host)).status);
}

/** Runs one agent (SPEC section 4) to the end and returns its final object, also written as the last line of
 * `<runs>/<runId>.jsonl`, and passed to `host.report`. Never throws: any failure becomes status `error`. */
export async function runAgent(opts: RunOptions, host: AgentHost): Promise<Final> {
  let status: Status = "error";
  let reason: string | null = null;
  let steps = 0;
  let toolCalls = 0;
  let blockedCalls = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let llmMs = 0;
  let toolMs = 0;
  let finalText = "";
  const clients: Client[] = [];
  let runs: Runs | undefined;

  try {
    const workspace = path.resolve(opts.workspace);
    const taskDir = path.resolve(opts.task);
    const runsDir = path.resolve(opts.runs);
    fs.mkdirSync(runsDir, { recursive: true });
    runs = new Runs(path.join(runsDir, `${host.runId}.jsonl`));
    const system = fs.readFileSync(path.join(ROOT, "shared", "system.txt"), "utf8");
    const taskText = fs.readFileSync(path.join(taskDir, "TASK.md"), "utf8");
    // Start both servers, and get the model client while they start.
    const t = host.transports(workspace, path.join(taskDir, "task.json"));
    const files = connect(t.files);
    const shell = connect(t.shell);
    clients.push(files.client, shell.client);
    const modelP = host.model();
    modelP.catch(() => {});
    await Promise.all([files.ready, shell.ready]);
    const [fl, sl] = await Promise.all([files.client.listTools(), shell.client.listTools()]);

    const tools: ToolDef[] = [];
    const owner = new Map<string, Client>();
    for (const [list, client] of [
      [fl.tools, files.client],
      [sl.tools, shell.client],
    ] as const) {
      for (const t of list) {
        const def: ToolDef = { name: t.name, input_schema: t.inputSchema };
        if (t.description !== undefined) def.description = t.description;
        tools.push(def);
        if (!owner.has(t.name)) owner.set(t.name, client);
      }
    }
    const known = new Set(owner.keys());

    const { sdk, client: anthropic } = await modelP;

    const messages: { role: "user" | "assistant"; content: unknown }[] = [{ role: "user", content: taskText }];
    let finished = false;

    while (!finished) {
      if (steps >= MAX_STEPS) {
        status = "step_limit";
        reason = `step limit of ${MAX_STEPS} reached`;
        break;
      }
      steps++;
      const step = steps;

      // ---- model call, with retries ----
      const t0 = performance.now();
      let resp: { content: Block[]; stop_reason: string | null; usage: Record<string, unknown> } | undefined;
      let failure: unknown;
      for (let attempt = 0; ; attempt++) {
        try {
          resp = (await anthropic.messages.create({
            model: MODEL,
            max_tokens: MAX_TOKENS,
            system,
            messages,
            tools,
            output_config: { effort: "medium" },
          } as never)) as unknown as typeof resp;
          break;
        } catch (e) {
          if (attempt < RETRY_DELAYS_MS.length && isRetryable(e, sdk)) {
            process.stderr.write(`tinycoder: ${host.runId}: step ${step}: ${errText(e)}; retrying\n`);
            await sleep(RETRY_DELAYS_MS[attempt]);
            continue;
          }
          failure = e;
          break;
        }
      }
      const llmDur = performance.now() - t0;
      llmMs += llmDur;
      if (!resp) {
        status = "error";
        reason = errText(failure);
        break;
      }
      const usage = resp.usage ?? {};
      const inTok = Number(usage.input_tokens ?? 0) || 0;
      const outTok = Number(usage.output_tokens ?? 0) || 0;
      inputTokens += inTok;
      outputTokens += outTok;
      const stopReason = resp.stop_reason ?? "";
      runs.write({
        type: "llm",
        step,
        duration_ms: llmDur,
        input_tokens: inTok,
        output_tokens: outTok,
        cache_read_input_tokens: Number(usage.cache_read_input_tokens ?? 0) || 0,
        stop_reason: stopReason,
      });

      const content = Array.isArray(resp.content) ? resp.content : [];
      messages.push({ role: "assistant", content });
      finalText = textOf(content);

      if (stopReason === "end_turn") {
        status = "done";
        reason = null;
        finished = true;
        break;
      }
      if (stopReason !== "tool_use") {
        status = "error";
        reason = stopReason || "missing stop_reason";
        break;
      }

      // ---- tool calls ----
      const results: Record<string, unknown>[] = [];
      for (const block of content) {
        if (block.type !== "tool_use") continue;
        const name = String(block.name ?? "");
        const input = (block.input && typeof block.input === "object" ? block.input : {}) as Record<string, unknown>;
        const argsBytes = Buffer.byteLength(JSON.stringify(block.input ?? {}), "utf8");
        const tt = performance.now();
        toolCalls++;
        let text: string;
        let isError: boolean;
        const blockReason = hookReason(name, input, known);
        if (blockReason !== null) {
          blockedCalls++;
          text = `blocked by hook: ${blockReason}`;
          isError = true;
        } else {
          try {
            const r = await owner.get(name)!.callTool({ name, arguments: input }, undefined, {
              timeout: TOOL_TIMEOUT_MS,
            });
            const parts = Array.isArray(r.content) ? (r.content as Block[]) : [];
            text = parts
              .filter((p) => p.type === "text")
              .map((p) => String(p.text ?? ""))
              .join("\n");
            isError = r.isError === true;
          } catch (e) {
            text = `error: ${(e as Error)?.message ?? String(e)}`;
            isError = true;
          }
        }
        const dur = performance.now() - tt;
        toolMs += dur;
        runs.write({
          type: "tool",
          step,
          tool: name,
          blocked: blockReason !== null,
          ok: !isError,
          duration_ms: dur,
          args_bytes: argsBytes,
          result_bytes: Buffer.byteLength(text, "utf8"),
        });
        const result: Record<string, unknown> = { type: "tool_result", tool_use_id: block.id, content: text };
        if (isError) result.is_error = true;
        results.push(result);
      }
      messages.push({ role: "user", content: results });
    }
  } catch (e) {
    status = "error";
    reason = errText(e);
  }

  const final: Final = {
    status,
    reason,
    steps,
    tool_calls: toolCalls,
    blocked_calls: blockedCalls,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    wall_ms: performance.now() - host.t0,
    llm_ms: llmMs,
    tool_ms: toolMs,
    final_text: finalText,
  };
  runs?.write({ type: "final", ...final });
  try {
    host.report(final);
  } catch (e) {
    process.stderr.write(`tinycoder: ${host.runId}: cannot report result: ${(e as Error)?.message ?? e}\n`);
  }
  await Promise.allSettled(clients.map((c) => c.close()));
  return final;
}
