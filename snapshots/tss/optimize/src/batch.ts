// `tinycoder batch`: many agents in one process (SPEC-SCALE.md).
//
// Every agent gets its own `files` and `shell` servers, built with the MCP SDK's server API exactly as for
// `serve`, and its own MCP SDK `Client`, joined by the SDK's `InMemoryTransport` (no child process per server).
// Each server instance holds only its own workspace, task file and caches. All agents share one Anthropic client
// (and so Node's fetch connection pool); each request is still the agent's own, sent unchanged.
// Commands run by `run_tests` / `run_command` are still OS processes; a shared limiter caps how many run at once.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { makeClient, runAgent, type AgentHost, type Final, type Sdk } from "./agent.js";
import { createFilesServer, type CacheLimits } from "./files.js";
import { Limiter } from "./limit.js";
import { createShellServer } from "./shell.js";

const MAX_AGENTS = 2000;
const MAX_CONCURRENCY = 1000;

// Per-agent cache limits of the files server: much smaller than a stand-alone server's, since up to 1,000 of them
// share one heap. Caches only save repeated reads; correctness does not depend on them.
const BATCH_FILE_LIMITS: CacheLimits = { maxFile: 256 << 10, budget: 512 << 10 };

interface AgentSpec {
  run_id: string;
  task: string;
  workspace: string;
  runs: string;
}

class Invalid extends Error {}

/** `p` made absolute with symlinks resolved as far as the path exists (the rest re-appended), so two spellings of
 * one directory (e.g. /tmp and /private/tmp on macOS) compare equal. */
function canonical(p: string): string {
  const tail: string[] = [];
  for (let cur = path.resolve(p); ; cur = path.dirname(cur)) {
    try {
      return path.join(fs.realpathSync(cur), ...tail);
    } catch {
      if (path.dirname(cur) === cur) return path.resolve(p);
      tail.unshift(path.basename(cur));
    }
  }
}

function parseConcurrency(raw: string): number {
  if (!/^\d+$/.test(raw.trim())) throw new Invalid(`--concurrency must be an integer, got ${JSON.stringify(raw)}`);
  const n = Number(raw.trim());
  if (n < 1 || n > MAX_CONCURRENCY) throw new Invalid(`--concurrency must be between 1 and ${MAX_CONCURRENCY}`);
  return n;
}

function readManifest(file: string): AgentSpec[] {
  let raw: string;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch (e) {
    throw new Invalid(`cannot read manifest ${file}: ${(e as Error).message}`);
  }
  let doc: unknown;
  try {
    doc = JSON.parse(raw);
  } catch (e) {
    throw new Invalid(`manifest is not valid JSON: ${(e as Error).message}`);
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) throw new Invalid("manifest must be a JSON object");
  const agents = (doc as { agents?: unknown }).agents;
  if (!Array.isArray(agents)) throw new Invalid('manifest must have an "agents" array');
  if (agents.length < 1 || agents.length > MAX_AGENTS) {
    throw new Invalid(`manifest must list between 1 and ${MAX_AGENTS} agents, got ${agents.length}`);
  }
  const files = new Set<string>();
  return agents.map((a, i) => {
    if (!a || typeof a !== "object" || Array.isArray(a)) throw new Invalid(`agents[${i}] must be an object`);
    const o = a as Record<string, unknown>;
    for (const k of ["run_id", "task", "workspace", "runs"]) {
      if (typeof o[k] !== "string" || o[k] === "") throw new Invalid(`agents[${i}].${k} must be a non-empty string`);
    }
    const spec = { run_id: o.run_id, task: o.task, workspace: o.workspace, runs: o.runs } as AgentSpec;
    // The run id names the runs file, so it must be a plain file name.
    if (/[\/\\\0]/.test(spec.run_id) || spec.run_id === "." || spec.run_id === "..") {
      throw new Invalid(`agents[${i}].run_id must not contain path separators: ${JSON.stringify(spec.run_id)}`);
    }
    // Two agents writing the same runs file could not both get the runs file SPEC.md describes.
    const runsFile = path.join(canonical(spec.runs), `${spec.run_id}.jsonl`);
    if (files.has(runsFile)) throw new Invalid(`agents[${i}]: another agent already writes ${runsFile}`);
    files.add(runsFile);
    return spec;
  });
}

/** Writes all of `text` to stdout synchronously, waiting out a full non-blocking pipe. */
function writeStdout(text: string): void {
  const buf = Buffer.from(text, "utf8");
  let off = 0;
  while (off < buf.length) {
    try {
      off += fs.writeSync(1, buf, off, buf.length - off);
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "EAGAIN") throw e;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2);
    }
  }
}

// Child processes of run_tests / run_command (see src/limit.ts): at most one starting-up process per core, at most
// PROC_HARD in all (the OS limit on processes is per user, and test runs may start children of their own).
const PROC_YOUNG_MS = 1000;
const PROC_HARD = 256;

function procLimiter(): Limiter {
  const env = Number(process.env.TINYCODER_MAX_PROCS); // tuning override, not needed normally
  const soft = Number.isInteger(env) && env >= 1 ? env : Math.max(2, os.availableParallelism());
  return new Limiter(soft, Math.max(soft, PROC_HARD), PROC_YOUNG_MS);
}

/** `tinycoder batch`. Returns the process exit code. */
export async function batch(manifestFile: string, concurrencyRaw: string): Promise<number> {
  let agents: AgentSpec[];
  let concurrency: number;
  try {
    concurrency = parseConcurrency(concurrencyRaw);
    agents = readManifest(manifestFile);
  } catch (e) {
    if (!(e instanceof Invalid)) throw e;
    process.stderr.write(`tinycoder: invalid batch: ${e.message}\n`);
    return 1;
  }

  // A crash inside one agent must not end the others. Errors inside an agent's own code path are caught by
  // runAgent; anything escaping elsewhere (e.g. a stray callback) is logged here instead of killing the process.
  process.on("uncaughtException", (e) => process.stderr.write(`tinycoder: uncaught: ${e?.stack ?? e}\n`));
  process.on("unhandledRejection", (e) =>
    process.stderr.write(`tinycoder: unhandled rejection: ${(e as Error)?.stack ?? e}\n`),
  );

  const sdk = require("@anthropic-ai/sdk") as Sdk;
  // Make Node load its lazily loaded fetch implementation now (~20-40 ms on first use), not inside the first agent's
  // first model call. Nothing is sent anywhere.
  new Response(new Request("http://localhost/", { method: "POST", headers: new Headers({ a: "b" }), body: "{}" }).body);
  const model = Promise.resolve({ sdk, client: makeClient(sdk) }); // one client, one connection pool, for all agents
  const procs = procLimiter();
  const counts = { done: 0, step_limit: 0, error: 0 };

  const runOne = async (spec: AgentSpec): Promise<void> => {
    const host: AgentHost = {
      runId: spec.run_id,
      t0: performance.now(),
      transports(workspace, taskJson) {
        if (!fs.statSync(workspace, { throwIfNoEntry: false })?.isDirectory()) {
          throw new Error(`workspace is not a directory: ${workspace}`);
        }
        const filesServer = createFilesServer(workspace, BATCH_FILE_LIMITS);
        const shellServer = createShellServer(workspace, taskJson, { procs });
        const [filesClient, filesSide] = InMemoryTransport.createLinkedPair();
        const [shellClient, shellSide] = InMemoryTransport.createLinkedPair();
        void filesServer.connect(filesSide);
        void shellServer.connect(shellSide);
        return { files: filesClient, shell: shellClient };
      },
      model: () => model,
      report(final: Final) {
        counts[final.status]++;
        writeStdout(JSON.stringify({ ...final, run_id: spec.run_id }) + "\n");
      },
    };
    await runAgent({ task: spec.task, workspace: spec.workspace, runs: spec.runs }, host);
  };

  // At most `concurrency` agents in progress; they start in manifest order, the next one as soon as one finishes.
  let next = 0;
  const lane = async (): Promise<void> => {
    while (next < agents.length) {
      const spec = agents[next++];
      try {
        await runOne(spec);
      } catch (e) {
        // runAgent does not throw; this is only a last guard so one agent cannot stop its lane.
        process.stderr.write(`tinycoder: ${spec.run_id}: ${(e as Error)?.stack ?? e}\n`);
      }
    }
  };
  // The first agents are started one event-loop turn apart, not all in one tick: each agent's set-up (servers,
  // initialize, tools/list) is a chain of microtasks, so starting them all at once would hold every agent's first
  // model request until the last agent had been set up.
  const lanes: Promise<void>[] = [];
  for (let i = 0; i < Math.min(concurrency, agents.length); i++) {
    if (i > 0) await new Promise((r) => setImmediate(r));
    lanes.push(lane());
  }
  await Promise.all(lanes);
  process.stderr.write(
    `tinycoder: batch finished: ${agents.length} agents (done ${counts.done}, step_limit ${counts.step_limit}, ` +
      `error ${counts.error})\n`,
  );
  return 0;
}
