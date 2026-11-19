"""Freezes the harness: sha256 of every file that decides a measurement, written to FROZEN.json. Run from the repo root.

  /usr/bin/python3 tools/freeze.py           write FROZEN.json (once, after the dry run, before sitting 1)
  /usr/bin/python3 tools/freeze.py --check   exit 1 if anything changed (preflight runs this)

Covers the replay model, the recordings, the ramp harness, the task repos with their hidden tests, the round-1 build
binaries/sources read from Experiment 4, and the round-2 specs and checks. Changing any of them after sitting 1
means the earlier sittings no longer count; write it in HISTORY.md.
"""
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ramp'))
from builds import BUILDS  # noqa: E402

GLOBS = ['replay/*.py', 'recordings/**/*.json', 'ramp/*.py', 'live/*.py', 'tools/openrouter_relay.py', 'bench/**/*',
         'tests/**/*', 'SPEC.md', 'SPEC-SCALE.md', 'RULES.md', 'INTERFACE.md', 'shared/system.txt', 'prompts/*.txt']
SKIP = ('__pycache__', '.DS_Store')


def files():
    seen = set()
    for g in GLOBS:
        for p in ROOT.glob(g):
            if p.is_file() and not any(s in p.parts or p.name == s for s in SKIP):
                seen.add(p)
    for (lane, rnd), b in BUILDS.items():
        d = b['dir']
        if rnd == 1 or d.exists():   # round-2 builds are frozen once tools/build_scale.py has made them
            for p in list(d.glob('bin/*')) + [q for q in d.rglob('*') if q.relative_to(d).parts[:1] in (('src',), ('dist',))]:
                if p.is_file() and not any(s in p.parts for s in SKIP):
                    seen.add(p)
    return sorted(seen)


def digest():
    out = {}
    for p in files():
        key = str(p.relative_to(ROOT)) if ROOT in p.parents else 'EXP4/' + str(p.relative_to(BUILDS[('py', 1)]['dir'].parents[1]))
        out[key] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main():
    f = ROOT / 'FROZEN.json'
    now = digest()
    if '--check' not in sys.argv:
        f.write_text(json.dumps(now, indent=1, sort_keys=True))
        print('froze %d files -> FROZEN.json' % len(now))
        return
    if not f.exists():
        print('not frozen yet (run tools/freeze.py once after the dry run)')
        sys.exit(1)
    old = json.loads(f.read_text())
    changed = sorted(k for k in set(old) | set(now) if old.get(k) != now.get(k))
    if changed:
        print('changed since freeze: ' + ', '.join(changed[:20]) + (' …' if len(changed) > 20 else ''))
        sys.exit(1)
    print('frozen files unchanged (%d)' % len(now))


if __name__ == '__main__':
    main()
