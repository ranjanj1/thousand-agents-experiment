# 1,000 AI agents at once (Experiment 5)

How many tinycoder agents can one 8 GB M1 Mac run at the same time in Python, TypeScript and Rust, and what happens to
speed and memory on the way to 1,000? Sequel to Experiment 4 ("Is AI Killing Python?",
https://github.com/ranjanj1/tinycoder-experiment), which ran one agent at a time and only worked out 100 agents on paper.

- **Round 1:** Experiment 4's own builds, unchanged (one process per agent, each with two MCP server processes).
- **Round 2:** Claude Code (Opus 5.5, high effort) adds one command to each language's own code, `tinycoder batch`
  (SPEC-SCALE.md), so one program runs every agent. Same spec, same allowed libraries, hidden checks it never sees.
- **The model is replayed:** every agent gets a reply Claude Sonnet 5.5 really gave in Experiment 4, after the delay it
  really took (replay/). Every language gets the same replies at the same speed, so only the language's own overhead
  differs. A small live check (6 real agents per language, Claude Sonnet 5.5 through OpenRouter) shows the replay matches reality.

Read in order: `RUN.md` (how we run it), `SPEC-SCALE.md`, `STATUS.md`, `HISTORY.md`. The Experiment 4 files (SPEC.md,
RULES.md, INTERFACE.md, tests/, bench/tasks, bench/validate.py, the session recorder) are copied unchanged.

| Folder | Contents |
|---|---|
| `replay/` | `build_recordings.py` (Exp 4 runs -> recordings), `replay_api.py` (the replay model) |
| `recordings/` | 45 recorded runs: every reply, stop reason, token counts and measured delay |
| `ramp/` | `ramp.py` (one step: N agents), `sitting.py` (the whole ramp), `measure_lib.py` (memory/CPU sampler), `preflight.py`, `summarize.py`, `builds.py` |
| `live/` | OpenRouter credit check for the paid live step |
| `tests/` | Exp 4 visible checks + `check_scale.py`, `ramp_dev.py`, scripted practice recordings (go in the round-2 bundle) |
| `bench/` | Hidden: Exp 4's `validate.py`, `audit.py`, task repos with hidden tests, and `hidden_scale.py` |
| `tools/` | Session recorder, network filter, relay (from Exp 4), `make_bundle_scale.py`, `run_scale_steps.py`, `build_scale.py`, `freeze.py` |
| `prompts/` | The round-2 prompts: `scale.txt`, `scale-optimize.txt` |
| `results/` | `ramp/<sitting>/` step records, samples and replay logs; `summary.json` |


## Results (19 Nov 2026, 8 GB M1 MacBook Pro, three sittings per round, restart before each)

| | Round 1: separate programs per agent | Round 2: one program runs every agent |
|---|---|---|
| Python | passed 50, memory wall at 100 (peak 12.1 GB, swap guard stopped it) | 1,000 / 1,000 in all 3 sittings, peak 317 MB |
| TypeScript | passed 50, memory wall at 100 (peak 8.7 GB) | 1,000 / 1,000 in all 3 sittings, peak 384 MB |
| Rust | passed 250, stopped at 500 by macOS's 1,333-processes-per-user limit (memory was 2 GB) | 1,000 / 1,000 in all 3 sittings, peak 391 MB |

Every number comes from `results/summary.json` (medians of the three sittings). The model is replayed, so these are the
languages' own costs, not model speed or API behaviour.

**Live check:** 6 agents per language against real Claude Sonnet 5.5: 6/6 passed in each language
(`results/ramp/live6/`). Earlier, larger live attempts (50, 20 and 10 agents; `live/`, `live20/`, `live10/`) had some requests
refused by OpenRouter with HTTP 402 `billing_error` ("would exceed your available credits given your current in-flight
requests"): the service sets credit aside for each in-flight request, so many simultaneous requests need a large balance.
Those refusals are not code failures; they are kept here unedited.

## What is and is not published

Published: the harness, the specs, the prompts Claude Code received, the code Claude Code wrote at each step
(`snapshots/*/scale`, `snapshots/*/optimize`), its own `NOTES.md` / `BUILD.md`, the hidden checks, the recorded model
replies and every step record. Not published: the raw Claude Code sessions (they contain request captures and system
prompts), compiled binaries and build caches. Home-directory paths and the list of apps running on the test Mac were removed
from the results before publishing.
