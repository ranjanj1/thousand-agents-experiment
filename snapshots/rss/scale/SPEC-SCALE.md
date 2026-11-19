# tinycoder at scale: build spec, round 2 (Experiment 5; draft until frozen)

Your folder holds your own tinycoder from Experiment 4. Everything in SPEC.md still holds and must keep working,
unchanged: `serve files`, `serve shell` and `run`. Add one command, so that one program can run many agents at once.
Where this file is silent, choose the simplest thing that works and write the choice down in NOTES.md.

## 1. Command

`tinycoder batch --manifest <file.json> --concurrency <N>`

The manifest is `{"agents": [{"run_id": str, "task": "<task-dir>", "workspace": "<dir>", "runs": "<dir>"}, ...]}`
with between 1 and 2,000 agents. `--concurrency` is between 1 and 1,000.

## 2. What it does

- It runs every agent in the manifest. At most N agents are in progress at any moment (started and not yet
  finished). Agents start in manifest order; when one finishes, the next starts.
- Each agent does exactly what `tinycoder run --task <task> --workspace <workspace> --runs <runs>` does in SPEC.md
  section 4: the same model requests, the same retry rules, the same hooks, the same step limit, the same runs file
  `<runs>/<run_id>.jsonl`, the same final object. Its run-id is the manifest's `run_id` (`TINYCODER_RUN_ID` is
  not used by `batch`).
- **MCP:** every agent has its own `files` server for its own workspace and its own `shell` server for its own task,
  built with the server API of your lane's official MCP SDK, and talks to them with the client API of the same SDK.
  The transport is your choice: stdio child processes as in SPEC.md, or an in-process transport that the SDK itself
  provides. Hand-written MCP protocol code (framing, `initialize`, `tools/list`, `tools/call`) is still not allowed.
- **Isolation:** an agent never reads, lists, searches or changes another agent's workspace, and one agent's tool
  calls never see another agent's state. SPEC.md section 2's path rules apply to each workspace on its own.
- `run_tests` and `run_command` still run their commands as operating-system processes, as SPEC.md section 3 says.
- **Model calls:** all agents may share one HTTP client and its connection pool, and requests for different agents
  may be in flight at the same time. Each request is exactly the request SPEC.md section 4.1 describes for that
  agent: never merged with another agent's, batched (no Message Batches API), cached, reordered or changed.
- **Failures stay local:** an API error, a step limit, a tool error or a crash inside one agent ends that agent only,
  with the status SPEC.md gives; every other agent goes on.

## 3. Output

- stdout: one line of JSON per agent, written when that agent finishes: the final object of SPEC.md section 4.5 with
  `"run_id"` added. Lines come in finish order. Nothing else goes to stdout; logs go to stderr.
- The runs file of each agent is exactly as SPEC.md section 4.4 says (its last line is the final object with
  `"type": "final"`, without `run_id`).
- Exit code 0 when every agent has finished (whatever its status) and printed its line. Exit code 1, with no agent
  started, when the manifest cannot be read or is invalid.

## 4. How it is measured

The harness starts `batch` with up to 1,000 agents against a fake Messages API that answers like Claude Sonnet 5.5
did (recorded replies, recorded delays). It measures from outside: memory (footprint and resident) of the program and
every process it starts, CPU, the time each agent spends outside the model, the whole batch's time, and whether each
agent's workspace passes its tests and hidden tests. `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` are set as in SPEC.md.
Operating-system limits (processes, open files) are the same for every lane and are not raised for you.

## 5. Tests you get

- `python3 tests/check.py bin/tinycoder`: the Experiment 4 checks; `run` and both servers must still pass them.
- `python3 tests/check_scale.py bin/tinycoder`: visible checks for `batch` (a replay model on ports 18401-18408).
- `python3 tests/ramp_dev.py bin/tinycoder`: the development ramp (optimize step only): 10 to 500 agents on the
  practice tasks, with memory, CPU and time outside the model.

Passing them is required but does not prove the spec is met: other checks you will not see are run later.
