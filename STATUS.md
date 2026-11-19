# Status
14 Nov 2026: step 0 done (45 recordings, 0 problems). Step 1 dry runs done (they never count):
- dry1, 10 agents at real model speed: 10/10 hidden passes in every language; replay 0 problems.
  Outside the model per agent: py 2.36 s, ts 0.71 s, rs 0.09 s (1 agent alone: 0.60 / 0.19 / 0.08 s, = Exp 4).
  Memory per agent (agent + 2 MCP servers): py 144 MB, ts 103 MB, rs 6.6 MB.
- dry2, 50 agents, no model delay: py stopped by the swap guard (peak 7.1 GB, swap +2.1 GB, all killed);
  ts 50/50 (peak 4.8 GB, pressure warning); rs 50/50 (peak 287 MB). Mac had 4.1 GB swap and apps open.
Step 2 decided with Ranjan: somaxconn 4096 for every run (redo after each restart); round 1 at the macOS default process limits.
Step 3: frozen, 133 files (FROZEN.json).
Next: restart, quit apps, somaxconn, sitting 1 (round 1).
Open decisions: which round goes live; OpenRouter key expires 18 Nov.

## Round 1, sitting 1 (14 Nov 2026, 17:12–17:32)
Last level where every agent passed: py 50, ts 50, rs 250.
- py: 10/25/50 pass (146 MB per agent; 50 agents 7.2 GB, swap +0.9 GB, 21 s outside the model each);
  100 stopped by the swap guard after 137 s (12.4 GB, pressure critical).
- ts: 10/25/50 pass (~100 MB per agent; 50 agents 4.8 GB); 100 stopped by the swap guard after 14 s (8.8 GB).
- rs: 10–250 pass (5.7 MB per agent; 250 agents 1.4 GB, 756 processes, 0.055 s outside the model);
  500: 348 passed, 152 could not start a process ("Resource temporarily unavailable"): the macOS limit of
  1,333 processes per user, not memory (peak 2.0 GB).
Next: restart, somaxconn, sitting 2.

## Round 1, sitting 2 (14 Nov 2026, ~17:40–17:50): same walls as sitting 1
py 50 (100: swap guard after 145 s, 12.0 GB), ts 50 (100: swap guard after 10 s, 9.1 GB),
rs 250 (500: 356 passed, 144 hit the process limit, errno 35; peak 2.0 GB).
Next: restart, somaxconn, sitting 3.

## Round 1, sitting 3 (14 Nov 2026, ~17:57–18:07): same walls again. ROUND 1 DONE.
3/3 sittings agree: py and ts pass 50, run out of memory at 100; rs passes 250, hits the macOS process limit at
500 (348 / 356 / 358 of 500 started). Medians in results/summary.json (ramp/summarize.py).
Next: round 2 (RUN.md step 5): make_bundle_scale, run_scale_steps (Claude Code, Opus 5.5 high), build_scale.

Units (checked 15 Nov): every "MB" in the step records is MiB (bytes / 1,048,576), like Activity Monitor, and the
Mac's 8 GB is 8,192 MiB. GB = MB / 1024: py 50 agents 7.0 GB (not 7.2), py 100 peak 12.1 GB (not 12.4), ts 100 peak
8.7 GB (not 8.8/8.9), py 25 3.5 GB. The film uses these.

## Round 2 (19 Nov 2026): DONE, 3/3 sittings
py, ts, rs all 1,000/1,000 in every sitting, no walls, swap +0. Peaks at 1,000: py 317 MB, ts 384 MB, rs 391 MB.
Outside the model at 1,000: py 3.3 s, ts 2.1 s, rs 2.0 s. summary.json rebuilt. Next: live check (new key + his OK).
