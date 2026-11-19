# NOTES: tinycoder, lane RS, round 2 (`batch`)

Rust 1.99, release build, dependencies only from the allowed list (`rmcp` 3.9, `tokio`, `serde`,
`serde_json`, `reqwest`, `regex`, `anyhow`, `clap`), vendored offline. No `unsafe` beyond two
libc declarations (`kill`, already there; `getrlimit`/`setrlimit`, new).

## Commands I ran

```sh
# baseline (the Experiment 4 code as found; bin/ held only tinycoder-agent, no bin/tinycoder)
cargo build --release --offline
mkdir -p bin && cp target/release/tinycoder target/release/tinycoder-agent bin/
python3 tests/check.py bin/tinycoder                    # 11/11 before any change

# after the changes (repeated after each step)
cargo build --release --offline
cp target/release/tinycoder target/release/tinycoder-agent bin/
cargo test --release --offline                          # 6 unit tests pass
python3 tests/check.py bin/tinycoder                    # 11/11 passed
python3 tests/check_scale.py bin/tinycoder              # ALL PASS (7/7)
python3 tests/ramp_dev.py bin/tinycoder                 # table below
python3 tests/bench_dev.py bin/tinycoder                # panic=unwind vs abort comparison

# clean-copy check of BUILD.md: rsync the folder without target/ and bin/ to /tmp, run the two
# BUILD.md commands there, then both test suites: all pass.
```

Plus ad-hoc experiments with a throwaway script outside the folder (since deleted). It imported
the `Replay` helper from `tests/check_scale.py`, ran `batch` under `/usr/bin/time -l` with
500/1,000 agents, and summarized the runs files per tool. I also ran edge cases by hand: empty,
non-object and missing manifests, a bad `run_id`, `--concurrency` 0 and 1001, a missing task
dir, a missing workspace, and an unreachable API.

What I read in `tests/`: `check_scale.py` and the output of `check.py`, to know how to run them
and what they expect, and the step/tool shape of the two practice recordings, to interpret the
timings. I did not read `replay_api.py`, `fake_api.py`, `ramp_dev.py` or `measure_lib.py`, and
changed nothing in `tests/`.

## Design

- **Entry points.** As in Experiment 4, `bin/tinycoder` is the lean binary: servers only, no
  TLS stack. `run` and now `batch` are exec'd into `bin/tinycoder-agent`. **BUILD.md was fixed**:
  it copied only `tinycoder`, but `tinycoder-agent` must sit next to it.
- **One agent core for both commands** (`src/agent.rs`, `run_agent`). `run` and `batch` call the
  same function with the same model requests, retry rules, hooks, step limit, runs file and final
  object. Only two things differ: how the servers are reached (`Servers::Stdio` or
  `Servers::InProcess`), and where the run-id comes from.
- **MCP in batch.** Each agent gets its own `FilesServer` (rooted at its workspace) and its own
  `ShellServer` (its task.json). Each server is served with rmcp's server API over rmcp's
  async-read/write transport on an in-memory `tokio::io::duplex` pipe. The agent talks to it with
  rmcp's client API (`().serve(...)`, `list_all_tools`, `call_tool`). rmcp does all framing and
  JSON-RPC (`initialize`, `tools/list`, `tools/call`); this is the in-process pattern rmcp's own
  transport tests use. No servers share state: caches live on the server instance, and paths are
  resolved against that instance's canonical root.
- **Scheduling** (`src/batch.rs`). One multi-threaded Tokio runtime (one worker per CPU) and a
  FIFO semaphore with N permits. The manifest is walked in order, each agent first taking a
  permit, so agents start in manifest order and at most N are in progress. The permit is
  released only after the agent's stdout line is written.
- **Model calls.** One `reqwest::Client` (one connection pool) is shared by all agents, and
  requests from different agents are in flight at once. Each request is built only from that
  agent's history.
- **History as JSON text.** This change also applies to `run`. `messages` is kept as one
  serialized JSON string instead of a `serde_json::Value` tree. Each request is
  `head + messages + tail`, where `head` (model, max_tokens, system) is computed once per
  program. The assistant `content` is appended as the **exact bytes the API returned**
  (`RawValue`), which satisfies "unchanged" more strictly than before (re-serializing a `Value`
  sorted object keys). It also avoids re-encoding the whole history at every step, and uses much
  less memory per agent.
- **Output.** Each agent's final object, with `"run_id"` added first, is written to stdout as one
  line (locked and flushed) when that agent finishes, so lines come in finish order. The runs
  file is exactly as in `run`: its last line is the final object plus `"type":"final"`, with no
  `run_id`.
- **Failures stay local.** API errors, step limits and tool errors already end only the
  affected agent. For crashes, the release profile changed from `panic = "abort"` to
  `panic = "unwind"`. Each agent's loop runs inside a small `CatchUnwind` future wrapper; a
  panic becomes `status: "error"`, `reason: "agent crashed: …"`, and the counters gathered so
  far are kept. Tool handlers in both servers are also wrapped, so a panic in a tool returns an
  `error: internal error: …` tool result instead of leaving the call hanging. Mutex poisoning is
  tolerated. If an agent task still dies without reporting, a fallback writes an error line for
  it to stdout and its runs file. bench_dev showed unwind vs abort makes no measurable startup
  difference (both ~4.2–4.8 ms, within noise) and costs ~0.3 MB RSS per server process.

## Choices where SPEC-SCALE.md is silent

- **Transport:** in-process (see above), not stdio children. 1,000 agents × 2 server processes
  would exceed this machine's process limit (`ulimit -u` 1333 soft / 2000 hard), and every
  agent would pay process start and pipe costs.
- **Concurrent child processes are capped at 2 × CPUs in batch** (16 here). Commands from all
  agents wait in FIFO order for a slot. The 30 s timeout and the reported `duration_ms` count
  from the command's own start, not from when it began waiting; the agent's `tool_ms` and the
  tool event's `duration_ms` include the wait. Reason: test commands are CPU-bound (one
  `python3 -m unittest` of the practice task costs ~0.15 s CPU). Running 500 at once on 8 cores
  thrashes: every run took ~30 s and hit the timeout, and peak memory of all processes was
  ~3.5 GB. With the cap, the same 500-agent run finishes with a median 7 s outside the model
  (cap 8 gave ~7.0 s, 16 gave ~7.3 s, 32 gave ~7.9 s, unlimited 30 s). I chose 2 × CPUs over
  1 × to leave room for tests that sleep or wait on I/O. `TINYCODER_PROCESS_SLOTS` overrides
  it; I used this for the measurements, and it is not needed in normal use. `serve shell` (one
  agent per process) has no cap, so `run` is unchanged.
- **Resource limits:** at start, `batch` raises its own *soft* `RLIMIT_NOFILE` and `RLIMIT_NPROC`
  up to the existing *hard* limits (never above them). If a spawn still fails with
  EAGAIN/EMFILE/ENFILE, it is retried with backoff (20 ms doubling to 500 ms, for up to 10 s)
  before it becomes a tool error.
- **files server caches in batch:** the search file-text cache is off (0 bytes; 8 MB per server
  in `serve files`), and the regex cache is 8 patterns instead of 64. This keeps memory per
  agent small; results are identical.
- **Manifest validation** (exit 1, nothing on stdout, nothing started). The manifest is
  invalid if: it is unreadable or not JSON; it is not an object with an `agents` array of 1 to
  2,000 objects; any `run_id`, `task`, `workspace` or `runs` is not a non-empty string; or a
  `run_id` contains `/` or NUL or is `.`/`..` (it names a file). Extra fields are ignored.
  Duplicate `run_id`s are not rejected. Problems with a single agent (missing task dir,
  missing workspace, unreadable task.json) are per-agent `error`s, not a manifest error.
- **`--concurrency`** outside 1–1000, or missing/unknown arguments to `batch`: exit 1 (clap's
  message on stderr), same as an invalid manifest. Other commands keep clap's usual exit 2.
- `TINYCODER_RUN_ID` is ignored by `batch`. `wall_ms` counts from when the agent got its slot.
  `runs` directories are created if missing, as in `run`. Retry and error logs on stderr are
  prefixed with `[run_id]`.

## Measurements (this machine: 8 CPUs, 8 GB)

`tests/ramp_dev.py`, final build:

| agents | done | batch s | outside ms (p50) | p95 ms | peak MB (all processes) | procs |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 10/10 | 5.7 | 233 | 243 | 7 | 1 |
| 50 | 50/50 | 6.4 | 737 | 899 | 239 | 17 |
| 200 | 200/200 | 9.2 | 2,246 | 3,650 | 266 | 17 |
| 500 | 500/500 | 19.5 | 7,276 | 13,392 | 385 | 17 |

Before the child-process cap, the 500 row was 26.9 s / 14,543 ms / 3,526 MB / 365 procs.

My own runs, with practice recordings at speed 1.0 and concurrency = agents:

- 1,000 agents: all `done` in 37.9 s. The batch process itself peaked at ~350 MB RSS
  (~0.35 MB per agent; much of it is rmcp's per-service channels and Tokio tasks, plus one
  HTTP connection per in-flight call). Time outside the model is dominated by the 1,000
  `python3` test runs sharing 8 cores (~190 CPU-s in total).
- The same 1,000 agents with the test command swapped for `true`: 5.7 s for the batch, a
  median 78 ms per agent outside the model, and 1.4 s user + 2.0 s sys CPU for the whole
  program. tinycoder's own overhead is ~3.5 ms of CPU per agent.

## Known limitations

- **Isolation is enforced for the file tools and hooks, not by the OS.** `run_command` allows
  `cat`, `python3`, etc. with any arguments (SPEC.md section 3). An agent's model could aim them
  at another workspace (`cat ../other/x`, `python3 -c …`), exactly as it could in `run`. The
  hooks are the same as in `run`, as SPEC-SCALE requires, so I did not add argv path checks;
  real separation would need OS sandboxing, which is outside the allowed toolset. In-process
  state (servers, caches, histories) is never shared.
- A command that hangs for the full 30 s holds one of the 16 process slots for that time, and
  enough such commands at once would delay other agents' commands.
- A crash that is not a Rust panic (stack overflow, abort, OOM kill) still takes down the
  whole batch.
- Stdout and the runs files are written with blocking writes from runtime threads. This is
  fine for line-sized writes, but a stalled stdout reader would stall that worker thread.
- Memory per agent (~0.35 MB) is bounded mostly by rmcp's internals (four services per agent:
  two clients, two servers), which I left as the SDK makes them.
- Visible checks pass; the hidden checks are not known.
