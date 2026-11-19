# tinycoder, lane PY: notes

Python 3.12.13 with the standard library, `mcp` 2.3.0 (official MCP SDK) and `anthropic` 2.8.0
(official SDK), all from the offline kit. Visible checks: **11/11 pass**
(`python3 tests/check.py bin/tinycoder`) and **7/7 pass** (`python3 tests/check_scale.py
bin/tinycoder`), both in this workspace and in a clean copy built with BUILD.md's commands.
Round 2 (SPEC-SCALE.md) added the `batch` command; see "Batch" below. BUILD.md is unchanged.

## Layout

| File | Role |
|---|---|
| `bin/tinycoder` | `sh` launcher. Picks the interpreter (see BUILD.md) and runs `src/tinycoder/main.py` with `-I` |
| `src/tinycoder/main.py` | argparse CLI: `serve files`, `serve shell`, `run`, `batch` |
| `src/tinycoder/mcpserve.py` | `build_server` (the SDK's **low-level `Server`** with our tools) and `serve` (runs it over `stdio_server`); argument validators |
| `src/tinycoder/files_server.py` | MCP server 1 (`list_files`, `read_file`, `write_file`, `edit_file`, `search`); `build()` for in-process use |
| `src/tinycoder/shell_server.py` | MCP server 2 (`run_tests`, `run_command`); `AsyncShell`/`build()`: the in-process variant for batch |
| `src/tinycoder/policy.py` | Command allowlist and the pre-call hook rules (used by the shell server and the agent) |
| `src/tinycoder/agent.py` | Agent loop: SDK `stdio_client` + `ClientSession` (run) or SDK in-process `Client` (batch) for both servers, `AsyncAnthropic` for the model |
| `src/tinycoder/batch.py` | `batch`: manifest validation, scheduling, shared model clients, one output line per agent |
| `src/tinycoder/fastimport.py`, `fastio.py` | Start-up helpers of `run`/`serve` (lazy SDK package `__init__`s, thread-free stdout) |

## Design

- **MCP is all SDK.** Servers use `mcp.server.lowlevel.Server(on_list_tools=..., on_call_tool=...)`
  over `mcp.server.stdio.stdio_server`. The agent uses `mcp.stdio_client` + `mcp.ClientSession`
  (`initialize`, `list_tools`, `call_tool`). There is no hand-written framing or JSON-RPC.
  I chose the low-level server over `MCPServer` so each tool's `input_schema` is exactly the JSON
  Schema I wrote, and so every failure (bad arguments included) comes back as an `isError` result
  whose text starts with `error: `, never as a protocol error. That server does not validate
  arguments, so each handler validates its own.
- **Same program for the children.** `run` spawns `sys.executable -I src/tinycoder/main.py serve ...`
  for each server, passing its own environment through. The SDK's default child env drops `LANG`,
  which the shell server must forward.
- **Concurrency.** Each server connection lives in its own anyio task (spawn → initialize →
  list_tools → wait for a stop event). The two servers boot in parallel, and in parallel the
  parent imports `anthropic` in a worker thread. At the end both shut down in parallel.
- **Model call.** `client.messages.with_raw_response.create(...)` with exactly `model`,
  `max_tokens`, `system`, `messages`, `tools`, `output_config` (checked by capturing the request
  body: no other keys, `anthropic-version: 2026-10-21`). I parse the **raw JSON**, so the
  assistant `content` is appended byte-for-byte, including thinking, redacted_thinking and unknown
  blocks or fields (checked).
- **Retries are my own** (SDK `max_retries=0`): HTTP 429/500/502/503/529 and `APIConnectionError`
  (which includes timeouts) are retried after 1, 2 and 4 s, so 4 attempts at most. Anything else
  fails the run at once. I did not use the SDK's built-in retry because it also retries
  408/409/504 and honours `x-should-retry`, which SPEC does not allow.
- **Hooks** run in the agent before any server sees the call, in this order: unknown tool name,
  arguments not an object, `path` absolute (`/`, `\`, `C:`) or containing a `..` part (split on `/`
  and `\`), `write_file` content over 100,000 UTF-8 bytes, `run_command` argv not allowed.
- **Speed.** Byte-compiled source (BUILD.md); lazy imports, so the servers never load `anthropic`;
  the no-op OpenTelemetry server middleware removed; `os._exit` after all output is flushed, which
  skips interpreter teardown (server exit after stdin EOF went from ~55 ms to ~2 ms, and `run`
  shutdown from ~180 ms to ~12 ms).

## Choices where SPEC.md was silent

- **Path display.** Output paths are relative to the workspace, use `/`, and are the lexically
  normalized path you passed, not the symlink-resolved one.
- **`.git` rule.** I reject any path component equal to `.git` (case-insensitive, for macOS),
  checked both as given and after resolving symlinks. So a symlink that points into `.git` is
  rejected too.
- **`list_files`.** The given directory itself is not listed. Directories report `size: 0`.
  Symlinks are listed under their target's type when the target is inside the workspace (and not
  in `.git`), otherwise skipped. Symlinked directories are not descended into, which avoids loops.
  A path that is a file is an error (`not a directory`). `truncated` appears only when true. The
  sort is a plain string sort of the path.
- **`read_file`.** Lines are split on `\n` only, and a trailing newline does not count as an
  extra line (`"a\nb\n"` has 2 lines). Line endings in `content` are kept exactly. With no range,
  `content` is the whole file verbatim. If `start_line` is past the end, `content` is `""` (not an
  error). `end_line` past the end is clamped. `end_line < start_line` is an error.
- **`write_file` / `edit_file`.** Content is written as exact UTF-8 bytes, with no newline
  translation. An empty `old_text` is an error. `edit_file` on a non-UTF-8 file is an error.
- **`search`.** Uses Python `re` syntax and `re.search` on each line. A trailing `\r` is stripped
  from the line `text`. A path may be a file or a directory. Files go in path order, and a match
  through a symlink is reported under the symlink's path.
- **Shell.** The child env is exactly the `PATH`, `HOME` and `LANG` that are set (any missing one is
  left out). Each child runs in its own session/process group, so a timeout `SIGKILL`s the whole
  tree, and stdin is `/dev/null`. Decoding is UTF-8 with replacement. `run_tests` merges stderr
  into the same pipe, so `output` is truly interleaved. A child killed by a signal reports the
  negative signal number, and the 30 s timeout reports `-1`. `run_command` checks the allowlist
  again on the server side. `argv[0]` must be the bare name (`/bin/ls` is refused). `test_command`
  is read from `task.json` once, on first use.
- **Step limit.** The tool calls requested in step 20 are still run, and their results are
  recorded, before the run ends with `step_limit` (20 model calls at most). Reaching end_turn at
  step 20 is `done`.
- **`reason`.** It is `null` for `done` and `step_limit`. For a bad stop reason it is the raw
  `stop_reason` (e.g. `"max_tokens"`). For API failures it is `"api error: HTTP <code>:
  <message>"` or `"api connection error: ..."`, and for other failures `"<ExceptionType>: <msg>"`.
  `final_text` is `""` unless the status is `done`.
- **Counters.** A failed model call (after its retries) still counts as a step. There is one `llm`
  line per step, written only when the call succeeds, and its `duration_ms` includes retry waits.
  `input_tokens`/`output_tokens` are sums of the API `usage` fields; cache tokens are only in the
  `llm` lines (null → 0). `wall_ms` runs from interpreter start (just after `main.py` loads) to
  the final line, including server startup and shutdown. `llm_ms` and `tool_ms` are sums over calls.
  All `*_ms` are floats rounded to 3 decimals; the shell server's `duration_ms` is an int, as SPEC says.
- **Log byte counts.** `args_bytes` is the UTF-8 length of the compact JSON of the arguments
  (`separators=(",", ":")`, non-ASCII kept). `result_bytes` is the UTF-8 length of the result text
  sent to the model.
- **`tool_result`.** `content` is the server's text, as a string. `is_error: true` is present only
  on errors. A `tool_use` stop with no `tool_use` blocks ends the run as `error`.
- **API key.** If `ANTHROPIC_API_KEY` is unset, the literal string `unset` is sent, so a fake API
  still works and the real API returns 401 (status `error`). SDK 2.8 sends the key as
  `Authorization: Bearer`, its own default.

## Known limitations

- **Startup cost.** Most of it is SDK import time. A server takes ~320 ms from spawn to its
  `initialize` reply: importing any `mcp` submodule runs `mcp/__init__.py` (~290 ms of pydantic
  models, client, auth and HTTP code). `run` reaches its first model request after ~0.75–1.0 s,
  mostly `import mcp` plus `import anthropic` (~550 ms), which the GIL forces to run one after the
  other. I measured that skipping the package `__init__` files with `sys.modules` stubs saves only
  ~60 ms and depends on SDK internals, so I did not ship it.
- **Memory.** Each Python process loads the SDK's pydantic models (three processes during `run`).
- **Env vars on macOS.** The OS adds `__CF_USER_TEXT_ENCODING` to every child's environment. We
  pass exactly PATH/HOME/LANG, and this one is outside our control.
- **Interpreter lookup.** The launcher hard-codes the kit venv path as a fallback, because the kit's
  `python3` on PATH cannot import `mcp` (see BUILD.md). Elsewhere, set `TINYCODER_PYTHON` or create
  `./.venv`.
- **Not tested here.** The real API and `tests/bench_dev.py`. There is no network, and the sandbox
  refuses even loopback TCP from my own processes.
- **Process tree.** A process that leaves its process group (e.g. calls `setsid`) can outlive a
  timeout. Its output is then dropped after a 5 s grace period.

## Batch (SPEC-SCALE.md)

`tinycoder batch --manifest <file.json> --concurrency <N>` runs every agent of the manifest in
**one process on one asyncio event loop** (anyio on asyncio, as the SDKs use).

### Design

- **Same agent.** Each agent is the `Agent` class that `run` uses, so the model request, retry
  rules, hooks, step limit, runs file and final object are the same code. `Agent.execute()`
  returns the final object; `run` prints it as before, and `batch` prints it with `run_id` added.
  `run` itself is unchanged in behaviour (11/11 checks still pass).
- **MCP in-process, all SDK.** Per agent, `files_server.build(workspace)` and
  `shell_server.build(workspace, task.json, limiter)` create two new low-level SDK `Server`
  objects (the same `build_server` the stdio servers use, so the same tools, schemas and
  `error: ` texts). The agent connects to each with the SDK client `mcp.Client(server,
  cache=None)`. For an in-process `Server` the SDK wires them through its own
  `DirectDispatcher` (`mcp.client.client._connect_inproc`). There is no child process and no
  framing, and none of the protocol code is mine. Tools are listed with `client.list_tools()`
  (paginated), files first then shell, and called with `client.call_tool()`. I checked that both
  give the same results as the stdio servers for normal calls, bad arguments and unknown tools.
  I did not use stdio children: 1,000 agents would need 2,000 Python server processes (about
  50 MB each), and this machine allows 1,333 processes per user.
- **Isolation.** Every agent has its own two `Server` objects, its own `Files` (rooted at its
  own `realpath(workspace)`) and its own `AsyncShell` (cwd = its own workspace). The servers keep
  no global or shared state, and SPEC section 2's path rules run per workspace. Agents share
  only the model HTTP clients and the command limiter.
- **Commands are OS processes.** `AsyncShell` cannot `chdir` the shared process, so
  `run_process_async` runs `os.posix_spawn` with the working directory changed only around
  the spawn call (`os.chdir(workspace)` → spawn → `os.fchdir(back)`). That window has no
  `await` and runs on the event-loop thread, so no other agent's code runs inside it. To keep it
  safe, all agent paths (task, workspace, runs) are made absolute up front. I chose this over
  `subprocess`/asyncio subprocesses because they fork+exec the big batch process: about
  5.3 ms of blocked event loop per command, against 0.1 ms for `posix_spawn` (measured in a
  250 MB process). Pipes are read by the event loop (`loop.add_reader`, non-blocking fds, last
  64 kB kept). The exit status comes from `waitpid(WNOHANG)` polled with back-off once the
  pipes reach EOF, so there is no thread per child. Everything else matches the stdio server:
  argv straight to the OS, env only PATH/HOME/LANG, stdin `/dev/null`, own session, a 30 s
  timeout that SIGKILLs the process group (`exit_code -1`), combined stdout+stderr for
  `run_tests`, and 8,000-character tails.
- **Model clients.** Every agent's request is built exactly as in `run`, through the same
  `_call_model` and our own retry loop (SDK `max_retries=0`). There is no merging, batching,
  caching or reordering. One finding shaped this: httpcore's pool scans, and `poll()`s, every
  pooled connection each time it hands one out. With one client and a 1,000-connection pool,
  that made each request O(pool), about 12 s of event-loop CPU in a 24 s, 500-agent run.
  So agents share clients in groups instead: worker slot *k* uses client *k // 8*, and each
  client's pool holds 8 connections (`max_connections = max_keepalive = 8`). Each worker runs one
  agent at a time with at most one request in flight, so a request never waits for a
  connection. The TLS context is the SDK default (`httpcore2.default_ssl_context()`), built once
  and shared by all clients. All clients are closed at the end.
- **Scheduling.** `min(N, len(agents))` worker tasks take the next manifest entry from a shared
  index when they become free. So agents start in manifest order, at most N are in progress,
  and the next one starts as soon as one finishes. A finished agent's line is written to stdout
  and flushed at once (finish order).
- **Failures stay local.** `Agent.execute()` turns every `Exception` into an `error` final
  object (API errors after retries, a missing workspace or TASK.md, a runs folder that cannot
  be created, server or tool failures). `run_one` has one more guard around it, so one agent can
  never end the task group. Each agent's tool calls are bounded by the 120 s MCP read timeout,
  and each command by its 30 s timeout.
- **Memory/CPU.** The SDKs are imported once. After start-up the batch runs `gc.collect()` +
  `gc.freeze()`, so the long-lived SDK objects leave the collector's view, and young-generation
  collections run less often (`gc.set_threshold(10000, 20, 50)`). The soft open-files limit is
  raised towards the hard limit, never above it. The OS limits themselves are not touched (here
  it was already 1,048,576).

### Choices where SPEC-SCALE.md was silent

- **Invalid manifest → exit 1, empty stdout, reason on stderr.** These count as invalid: the
  file is unreadable or not UTF-8 JSON; it is not an object with an `agents` array; there are
  fewer than 1 or more than 2,000 agents; an entry is not an object; `run_id`/`task`/`workspace`/`runs`
  is missing, not a string, or empty; a `run_id` contains `/`, `\` or NUL, or is `.`/`..` (it
  becomes a file name); two entries have the same `run_id` *and* the same runs folder (they
  would write one file). Extra keys are ignored. `--concurrency` that is not an integer from 1 to
  1,000, or missing arguments, also exit 1 (argparse's usual 2 is mapped to 1 for `batch`).
- **Paths are not checked up front.** A missing task, workspace or unwritable runs folder fails
  only that agent (`status: error`) and the batch goes on. Relative paths are resolved
  against the batch's working directory.
- **`wall_ms`** of a batch agent runs from the moment that agent starts (its worker picks it
  up) to its final line. Time spent waiting in the queue is not counted.
- **Command slots.** At most `max(16, 4 × CPU count)` commands (32 on this 8-core machine) run
  at once across all agents, FIFO. Waiting for a slot is part of the tool call's time but not of
  its 30 s timeout or `duration_ms`. Why: test runs are CPU-bound, and when hundreds run at once
  all of them finish near the end of the burst. Measured at 500 agents, 256 slots gave a
  2.0 s median time outside the model, 32 slots 1.4 s, 12 slots 1.4 s. The cap also keeps the
  batch far below the 1,333-process limit. A spawn refused with `EAGAIN` is retried with
  back-off for up to 10 s. `TINYCODER_COMMAND_SLOTS` overrides the cap (for experiments).
- **`TINYCODER_RUN_ID`** is ignored by `batch`, as SPEC-SCALE says.
- **stderr** gets retry notices and SDK warnings, as with `run`.

### Measurements (this machine: 8 cores, replay API on loopback)

`python3 tests/ramp_dev.py bin/tinycoder`, first version (one client, 256 slots, asyncio
subprocess) → final:

| agents | batch s | outside ms (median) | p95 ms | peak MB | CPU % |
|---|---|---|---|---|---|
| 10 | 6.2 → 6.3 | 83 → 85 | 96 → 92 | 91 → 91 | 14 → 14 |
| 50 | 6.5 → 6.4 | 226 → 194 | 246 → 233 | 145 → 109 | 23 → 19 |
| 200 | 9.7 → 7.4 | 1598 → 589 | 1879 → 720 | 259 → 192 | 41 → 99 |
| 500 | 19.0 → 9.0 | 6194 → 1357 | 8615 → 2169 | 276 → 253 | 56 → 128 |

My own runs on the same replay API: 1,000 agents at `--concurrency 1000` finished 1,000/1,000
`done` in 13.2 s with 271 MB peak RSS. 2,000 agents at concurrency 1,000 finished 2,000/2,000 in
23.7 s with 296 MB. The batch process itself used about 40% of one core (500 agents: 3.7 s CPU in
9.1 s), while the test processes used 24 s CPU. Most of the remaining time outside the model is
those test runs competing for the 8 cores.

### Limitations

- **One event loop.** The `files` tools are synchronous (as in the stdio server), so one
  `search` or `list_files` over a very large workspace stalls every agent for its duration.
  When hundreds of model replies arrive at once, an agent's next tool call can wait about
  60–300 ms for the loop. I measured the process at about 40% of a core and did not shard into
  several processes, which would cost about 60–90 MB per extra Python process.
- **`posix_spawn` holds the GIL** and, while all cores are busy with test processes, it took up
  to about 4.5 ms per call on the loop thread. That is still cheaper than fork+exec.
- **Working-directory window.** Correct as long as no other thread in the batch depends on the
  cwd. Ours don't, but a future SDK version that resolves relative paths in a background thread
  could be affected.
- **No per-agent cancellation** beyond the SDK/MCP timeouts. A hung model connection holds its
  slot until the SDK's own 600 s timeout.
- **Fake API only.** Not run against the real API (no network here). The replay server was
  reachable only on the check ports (18401-18408); other loopback ports were refused in this
  sandbox.

## How I verified (commands)

Following RULES.md rule 4, I only ran the files in `tests/` and never opened their source. The
practice task folders were used only as inputs.

```sh
# environment / SDK discovery
python3 --version; ls /Users/Shared/tc-kit/pyenv/lib/python3.12/site-packages
/Users/Shared/tc-kit/pyenv/bin/python3 -c "import mcp, anthropic"     # works (bin-py/python3 does not)
# read SDK source: mcp/server/lowlevel/server.py, mcp/server/stdio.py, mcp/client/stdio.py,
#   mcp/client/session.py, anthropic/_client.py, anthropic/resources/messages/messages.py
/Users/Shared/tc-kit/pyenv/bin/python3 -X importtime -c "import mcp.server.lowlevel"   # startup profiling

# visible checks
chmod +x bin/tinycoder && python3 tests/check.py bin/tinycoder          # 11/11
# clean-copy rehearsal of the harness
cp -R . /tmp/tc-clean && cd /tmp/tc-clean && chmod +x bin/tinycoder \
  && /Users/Shared/tc-kit/pyenv/bin/python3 -m compileall -q src && python3 tests/check.py bin/tinycoder
```

Round 2 (batch). As before I only ran the files in `tests/` and did not open their source;
the practice task folders were used only as inputs (`task.json` to see the test command).

```sh
# SDK discovery for the in-process transport and the HTTP pool
cat/sed/grep $SP/mcp/client/client.py $SP/mcp/client/_memory.py $SP/mcp/shared/direct_dispatcher.py \
     $SP/mcp/server/runner.py $SP/anthropic/_base_client.py $SP/httpx2/_config.py $SP/httpcore2/_ssl.py
ulimit -a                                                   # 1,333 processes, 1,048,576 files
python3 tests/check.py bin/tinycoder                        # 11/11
python3 tests/check_scale.py bin/tinycoder                  # 7/7 (0/7 before)
python3 tests/ramp_dev.py bin/tinycoder                     # table above
# profiling: replay API on a check port + a manifest of copied practice repos
python3 tests/replay_api.py --recordings tests/recordings --log /tmp/tcprof/api.log --port 18405
python3 /tmp/tcprof/setup.py 500                            # copies repos, writes manifest
/Users/Shared/tc-kit/pyenv/bin/python3 /tmp/tcprof/prof.py batch --manifest ... --concurrency 500  # cProfile
/tmp/tcprof/bench.sh 500 {256,32,12}                        # command-slot comparison
/Users/Shared/tc-kit/pyenv/bin/python3 -I /tmp/tcprof/self.py batch ... --concurrency 1000      # 1,000 and 2,000 agents
/Users/Shared/tc-kit/pyenv/bin/python3 /tmp/tcx/spawn.py   # fork+exec 5.28 ms vs posix_spawn 0.11 ms
/Users/Shared/tc-kit/pyenv/bin/python3 /tmp/tcx/gil.py     # posix_spawn does not release the GIL
/Users/Shared/tc-kit/pyenv/bin/python3 /tmp/tcx/probe.py   # in-process servers vs stdio results
/Users/Shared/tc-kit/pyenv/bin/python3 /tmp/tcx/probe2.py  # spawn runner: cwd, env, tails, timeout kills grandchild, signals, cancel
sh -c 'for a in ...; do bin/tinycoder batch $a; done'       # 11 invalid manifests/arguments -> exit 1, empty stdout
bin/tinycoder batch --manifest ok.json --concurrency 2      # missing workspace / TASK.md / runs dir: local errors, exit 0
# clean-copy rehearsal
cp -R ... /tmp/tc-clean && cd /tmp/tc-clean && chmod +x bin/tinycoder \
  && /Users/Shared/tc-kit/pyenv/bin/python3 -m compileall -q src \
  && python3 tests/check.py bin/tinycoder && python3 tests/check_scale.py bin/tinycoder   # 11/11, 7/7
```

Round 2 scratch files were in `/tmp/tcprof`, `/tmp/tcx` and `/tmp/tc-clean` (outside the
project, deleted afterwards).

Round 1 throwaway scripts in `/tmp/tc-scratch` (outside the project, since deleted):

- **`mock_run.py`** ran the real `Agent` and the real `anthropic` SDK over an in-process
  `httpx2.MockTransport`, used because loopback TCP is blocked here. It covered: request body
  keys and headers, tool order, thinking-block round-trip, hook blocks (`..`, `git push`, unknown
  tool), parallel tool results in one user message, a 529 then success, a 503 three times then
  success, 529/connection error on all 4 attempts, a 400 not retried, `max_tokens`, the step limit
  (20 requests, exit 2), a missing TASK.md, and a missing workspace (server dies, run reports
  `MCPError: Connection closed`, no hang).
- **`srv_test.py`** called both servers through the SDK client. It covered line ranges, non-UTF-8,
  absolute/`..`/symlink escapes, a symlink into `.git`, the 500/200 truncation, the regex `^`
  anchor, a bad regex, duplicate `old_text`, nested writes, env filtering, the
  stdout/stderr split, 8,000-character tails, refused commands, and the 30 s timeout killing a
  grandchild (`exit_code -1`).
- **`init_time.py`, `exit_time.py`, `phase.py`** measured startup, exit and per-phase timings.
