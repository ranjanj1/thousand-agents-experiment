"""Builds the round-2 starting folder for one lane: sessions/private/bundle-<lane>s/. Only these files reach a session.

  /usr/bin/python3 tools/make_bundle_scale.py py|ts|rs|all

Starts from the lane's own measured Experiment 4 code (snapshots/<lane>/optimize; TypeScript: its repair snapshot),
with build outputs removed, and adds: SPEC-SCALE.md, the round-2 CLAUDE.md, tests/check_scale.py, tests/ramp_dev.py,
the replay model and sampler (tests/replay_api.py, tests/measure_lib.py) and the scripted practice recordings.
Never copied: bench/ (hidden checks, real task repos) and recordings/ (real replies to the hidden tasks).
"""
import hashlib, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ramp'))
from builds import EXP4  # noqa: E402

KIT = Path('/Users/Shared/tc-kit')
SNAP = {'py': 'optimize', 'ts': 'repair', 'rs': 'optimize'}
ADD = {'SPEC-SCALE.md': 'SPEC-SCALE.md', 'CLAUDE-SCALE.md': 'CLAUDE.md', 'tests/check_scale.py': 'tests/check_scale.py',
       'tests/ramp_dev.py': 'tests/ramp_dev.py', 'replay/replay_api.py': 'tests/replay_api.py',
       'ramp/measure_lib.py': 'tests/measure_lib.py'}


def build(lane):
    out = ROOT / 'sessions' / 'private' / ('bundle-%ss' % lane)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(EXP4 / 'snapshots' / lane / SNAP[lane], out, symlinks=True)
    for junk in ('dist', 'target', 'node_modules', '.cargo-home', '.npm-cache', '.venv'):
        shutil.rmtree(out / junk, ignore_errors=True)
    for pc in list(out.rglob('__pycache__')):
        shutil.rmtree(pc)
    if lane == 'rs':
        (out / 'bin' / 'tinycoder').unlink(missing_ok=True)
    for src, dst in ADD.items():
        (out / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / src, out / dst)
    shutil.copytree(ROOT / 'tests' / 'recordings', out / 'tests' / 'recordings', dirs_exist_ok=True)
    if lane == 'ts':
        shutil.copytree(KIT / 'npm-cache', out / '.npm-cache', ignore=shutil.ignore_patterns('_logs'))
    if lane == 'rs':
        (out / '.cargo').mkdir(exist_ok=True)
        shutil.copy2(KIT / 'rs-vendor-config.toml', out / '.cargo' / 'config.toml')
    leaks = [p for p in out.rglob('*') if p.relative_to(out).parts[:1] == ('bench',) or p.name in ('validate.py', 'hidden_scale.py', 'solution')
             or any(t in p.name for t in ('t1-bugfix', 't2-feature', 't3-failing'))]
    assert not leaks, leaks
    hashes = ''.join('%s  %s\n' % (hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(out).as_posix())
                     for p in sorted(out.rglob('*')) if p.is_file() and '.npm-cache' not in p.parts)
    (out.parent / ('bundle-%ss.sha256' % lane)).write_text(hashes)
    print(lane, 'scale bundle:', sum(1 for p in out.rglob('*') if p.is_file()), 'files ->', out)


if __name__ == '__main__':
    for lane in (['py', 'ts', 'rs'] if sys.argv[1] == 'all' else [sys.argv[1]]):
        build(lane)
