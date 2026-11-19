"""Which tinycoder build each (lane, round) uses. Round 1 = Experiment 4's measured optimize builds, unchanged, read
from the Experiment 4 repo. Round 2 = the scale builds Claude Code makes in this experiment (builds/<lane>-scale/,
built from snapshots/<lane>s/<step>/ by tools/build_scale.py)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXP4 = Path(os.environ.get('TC_EXP4', str(ROOT.parent / 'tinycoder-experiment')))
KIT = '/Users/Shared/tc-kit'
# Same PATH for every lane at run time, so the task's python3 (run_tests, run_command) is the same interpreter.
RUN_PATH = '%s/bin-ts:%s/bin-py:/usr/bin:/bin' % (KIT, KIT)
LANES = ('py', 'ts', 'rs')

BUILDS = {}
for _l in LANES:
    BUILDS[(_l, 1)] = {'dir': EXP4 / 'builds' / ('%s-optimize' % _l), 'how': 'run', 'required': True,
                       'what': 'Experiment 4 optimize build, unchanged: one process per agent + 2 MCP server processes'}
    BUILDS[(_l, 2)] = {'dir': ROOT / 'builds' / ('%s-scale' % _l), 'how': 'batch', 'required': False,
                       'what': 'Round 2 scale build (SPEC-SCALE.md): one program runs every agent'}
