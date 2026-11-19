# Runbook: Experiment 5, 1,000 agents at once (we run this together)

Nothing here has been run yet. Every command runs from this folder (`~/Documents/Projects/thousand-agents`), with
`/usr/bin/python3` (Python 3.9, standard library only), outside the sandbox, unless it says otherwise. Steps marked
**PAID** spend OpenRouter credit and need Ranjan's yes first. The OpenRouter key **expires 18 Nov 2026**: the live
check must run by 17 Nov, or a new key is needed.

## 0. Once: data and checks (free, about 5 minutes)

```sh
/usr/bin/python3 replay/build_recordings.py            # 45 Exp 4 runs -> recordings/ (prints steps and model time per task)
/usr/bin/python3 ramp/preflight.py                     # what the Mac looks like now (changes nothing)
```

## 1. Dry run: 10 agents, all three languages (free, about 5 minutes)

The first real use of the harness. We look at every output file together before anything else.

```sh
for l in py ts rs; do /usr/bin/python3 ramp/ramp.py --lane $l --round 1 --n 10 --sitting dry1 --keep; done
```

Check: 10/10 hidden passes per language; `results/ramp/dry1/*.replay.jsonl` has no `problems`; the samples file
shows memory rising and falling; the agents' time outside the model is close to Experiment 4's (0.55 s Python,
0.20 s TypeScript, 0.09 s Rust per run). If something is wrong we fix it here: the dry runs never count.
Also try a quick stress run with no model delay (`--speed 0 --n 50 --sitting dry2`), to see the harness itself keeps up.

## 2. Mac limits: the same for every language (decide together)

What the Mac had on 13 Nov: `kern.maxproc` 2000, `kern.maxprocperuid` 1333, `kern.ipc.somaxconn` 128, open files
1,048,576 per shell. Round 1 needs **3 processes per agent** (agent + 2 MCP servers, + 1 more while it runs tests),
so at the default limits about **400 agents** fit in total, whatever the language.

- **The replay model's connection queue** (`kern.ipc.somaxconn`) belongs to our fake model, not to any language: raise
  it for all runs so 1,000 agents connecting at once are not dropped. `sudo sysctl -w kern.ipc.somaxconn=4096`
  (resets at restart; do it after each restart).
- **Process limit:** my recommendation is to run round 1 at the **macOS defaults**. Hitting "this Mac allows 1,333
  processes per user" is a real wall that every developer would hit, and it is the same for all three languages. If we
  also want to see the memory wall behind it, a second pass with `sudo sysctl -w kern.maxproc=4000
  kern.maxprocperuid=3500` (if the kernel accepts it) is labelled as such in the film.

## 3. Freeze (free)

After the dry run is clean, nothing that decides a measurement may change: `/usr/bin/python3 tools/freeze.py`.
`preflight.py --strict` (run by every sitting) refuses if anything changed afterwards.

## 4. Round 1: three sittings (free; about 1-2 hours each)

Before each sitting: **restart the Mac** (swap was 7.9 GB on 13 Nov; the preflight refuses above 1 GB), plug in
power, quit every app (Chrome, Remotion Studio, Docker…), turn off Low Power Mode, redo `kern.ipc.somaxconn`. Then:

```sh
caffeinate -i /usr/bin/python3 ramp/sitting.py 1 --round 1      # then 2, then 3, each after a fresh restart
```

Levels 10, 25, 50, 100, 250, 500, 1000; languages shuffled at each level; a language stops climbing after its
first failed step in that sitting. A step stops itself when swap grows by 2 GB, when memory pressure is critical for
10 s, or after 20 minutes, and every agent is killed. **Record the screen** during each sitting (Activity Monitor's
Memory tab + the terminal), for the film's cold open.

## 5. Round 2: Claude Code rebuilds each language for scale (subscription, no API cost)

```sh
/usr/bin/python3 tools/make_bundle_scale.py all                  # each lane's own Exp 4 code + SPEC-SCALE.md + checks
../tinycoder-experiment/.venv/bin/python tools/run_scale_steps.py           # prints the plan
../tinycoder-experiment/.venv/bin/python tools/run_scale_steps.py --go      # PY, TS, RS: scale step, then optimize step
/usr/bin/python3 tools/build_scale.py py   # then ts, rs: fresh build + SDK audit + Exp 4 hidden checks + round-2 hidden checks
```

Same model and effort as Experiment 4 (Opus 5.5, high), same sandbox, same 30-minute cap per step. A failed gate is a
result: it goes in HISTORY.md, and a repair round only with Ranjan's OK, as in Experiment 4. Then three sittings again:

```sh
caffeinate -i /usr/bin/python3 ramp/sitting.py 1 --round 2       # 2, 3 after fresh restarts
```

## 6. Summary (free)

```sh
/usr/bin/python3 ramp/summarize.py      # -> results/summary.json, the one data file the film reads
```

## 7. PAID: the live check (asks first, every time)

100 real agents per language against Claude Sonnet 5.5 (OpenRouter, provider pinned to Anthropic), to show the replay
matches reality. Experiment 4's runs cost about $0.026 each: **about $8 for all three languages**.

```sh
/usr/bin/python3 ramp/ramp.py --lane py --round 2 --n 100 --sitting live --api live --budget 3.5   # ts, rs the same
```

It checks the credit first (free) and refuses below budget + $1.50. Optional: 1,000 live agents for one language
(about $26) only if Ranjan wants that shot. Which round goes live (1 or 2) is decided after round 2.

## 8. After

HISTORY.md (every failure kept), STATUS.md, the public repo (fresh single-commit repo, the scrub from
LEARNINGS §7 in video-maker), then the film.

> **Outcome of the live step (19 Nov 2026):** 50, 20 and 10 simultaneous live agents had some requests refused by OpenRouter
> (HTTP 402, credit is set aside per in-flight request, about $0.5 each here); 6 agents per language ran cleanly (6/6 each).
> Run live steps with a balance of at least about 0.5 dollars times the number of agents in flight.
