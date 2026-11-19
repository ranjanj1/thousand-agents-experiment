# History (every step and every failure, kept)

## 13 Nov 2026: harness written, nothing run
- Plan agreed with Ranjan (video-maker/projects/thousand-agents/PLAN.md). He asked for everything to be written but
  not run: "We will run it together later."
- Copied unchanged from Experiment 4 (repo HEAD bfb9787): SPEC.md, RULES.md, INTERFACE.md, CLAUDE.md, KIT.md,
  shared/system.txt, tests/ (check.py, fake_api.py, mcp_client.py, bench_dev.py, tasks/), bench/validate.py,
  bench/audit.py, bench/tasks/, tools/record_session.py, tools/net_filter.py, tools/openrouter_relay.py, sandbox/.
- New: replay model, recordings builder, ramp harness and sampler, preflight, sitting runner, summary, live gate,
  SPEC-SCALE.md, round-2 prompts, visible and hidden round-2 checks, bundle/build/freeze tools, RUN.md.
- Found while writing (13 Nov): this Mac allows 1,333 processes per user (kern.maxprocperuid) and 2,000 in all; round 1
  needs 3 per agent, so ~400 agents is a hard ceiling for every language at default limits. kern.ipc.somaxconn 128.
  Swap was 7.4-7.9 GB in use and memory pressure "warning" with the usual apps open: a sitting needs a fresh restart.
- Only offline checks were done: every script compiles; the sampler read a test process tree correctly (libproc);
  build_recordings --dry validated the 45 Experiment 4 runs. No agent, replay server or model call has run.

## 14 Nov 2026: step 0, dry runs, limits, freeze
Recordings built (45, 0 problems). Dry runs dry1 (10 agents) and dry2 (50, no delay) as in STATUS.md; they never count.
Limits decided with Ranjan: kern.ipc.somaxconn 4096 for all runs; process limits left at the macOS defaults
(maxproc 2000, maxprocperuid 1333). Frozen: 133 files.
Sitting 1 (round 1), first start: crashed before the preflight (a missing bracket in sitting.py's preflight file
name), no agent ran. Fixed (file name only, nothing measured changes), re-frozen, restarted.
Round 1: sittings 1-3 run 14 Nov after a restart each (somaxconn 4096, defaults otherwise); identical walls in all three.

## 15 Nov 2026: round 2, first attempt set aside (harness bugs, not the lane's fault)
PY's scale session (14:01-14:31) hit the 30-minute cap fighting three visible checks that could never pass:
1. batch_isolation looked for "a * b" in practice-pass's calc.py, which already contains it.
2. batch_retry_per_agent put the 529 on the first call; the replay model treats a retried first call as a new agent
   (no reply id yet), so it answered 529 forever. The 529 is now on the second call (replay docstring says so).
3. all_lines_ok required an llm line from an agent whose only model call was a permanent API error.
Fixed checks pass on that session's own code (all 7). The attempt is kept in sessions/private/void/; PY's optimize
step had only just started and was stopped. Re-frozen, bundles rebuilt, round 2 restarted from PY scale.
Round 2 restarted 14:35: PY scale ALL PASS (17.7 min), PY optimize 30 min cap; TS scale ALL PASS then its own stress
tests until the 30 min cap, TS optimize 30 min cap; RS scale ALL PASS (29.2 min), RS optimize 30 min cap.
Builds (tools/build_scale.py, 17:33): all three pass the SDK audit, Exp 4's hidden checks and hidden_scale.py.
freeze.py now also covers the round-2 builds (bin, src, dist); re-frozen.
- TS round-2 NOTES.md was not updated (identical to Exp 4's): both TS sessions hit the 30-min cap first. Its design is in the comment at the top of src/batch.ts (SDK InMemoryTransport, one shared Anthropic client).
