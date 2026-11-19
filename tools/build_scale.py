"""Builds and checks a round-2 version: snapshots/<lane>s/<step>/ -> builds/<lane>-scale/, from scratch with the
commands of its own BUILD.md (offline kit), then the gates. Run from the repo root with /usr/bin/python3.

  /usr/bin/python3 tools/build_scale.py py|ts|rs [--step optimize]

Gates (all must pass before the ramp uses it): Experiment 4's SDK audit (bench/audit.py), Experiment 4's 26 hidden
checks on serve/run (bench/validate.py) and the round-2 hidden checks (bench/hidden_scale.py). Results go to
results/validation/<lane>-scale.json.
"""
import argparse, json, re, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ramp'))
from builds import KIT, RUN_PATH  # noqa: E402


def build_commands(d):
    m = re.search(r'```(?:sh|bash)\n(.*?)```', (d / 'BUILD.md').read_text(), re.S)
    if not m:
        sys.exit('no sh block in %s/BUILD.md' % d)
    return [l.split('#')[0].strip() for l in m.group(1).splitlines() if l.split('#')[0].strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('lane', choices=['py', 'ts', 'rs'])
    ap.add_argument('--step', default='optimize')
    o = ap.parse_args()
    snap = ROOT / 'snapshots' / (o.lane + 's') / o.step
    d = ROOT / 'builds' / ('%s-scale' % o.lane)
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(snap, d, symlinks=True)
    for junk in ('dist', 'target', 'node_modules', '.cargo-home', '.npm-cache'):
        shutil.rmtree(d / junk, ignore_errors=True)
    if o.lane == 'rs':
        (d / 'bin' / 'tinycoder').unlink(missing_ok=True)
        e = {'PATH': '%s/bin-rs:/usr/bin:/bin' % KIT, 'HOME': str(d), 'CARGO_HOME': str(d / '.cargo-home'), 'CARGO_NET_OFFLINE': 'true'}
    elif o.lane == 'ts':
        shutil.copytree(Path(KIT) / 'npm-cache', d / '.npm-cache')
        e = {'PATH': RUN_PATH, 'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8', 'npm_config_cache': str(d / '.npm-cache'),
             'npm_config_offline': 'true', 'npm_config_update_notifier': 'false', 'npm_config_audit': 'false', 'npm_config_fund': 'false'}
    else:
        e = {'PATH': RUN_PATH, 'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8'}
    times = {}
    for cmd in build_commands(d):
        t0 = time.time()
        p = subprocess.run(['/bin/sh', '-c', cmd], cwd=d, env=e, capture_output=True, text=True, timeout=1200)
        if p.returncode:
            sys.exit('build step failed: %s\n%s' % (cmd, (p.stdout + p.stderr)[-1500:]))
        times[cmd] = round(time.time() - t0, 2)
    shutil.rmtree(d / '.npm-cache', ignore_errors=True)
    binp = str(d / 'bin' / 'tinycoder')
    gates = {}
    for name, argv in [('audit', [sys.executable, 'bench/audit.py', o.lane.upper(), str(d)]),
                       ('exp4_hidden', [sys.executable, 'bench/validate.py', binp]),
                       ('scale_hidden', [sys.executable, 'bench/hidden_scale.py', binp])]:
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=7200)
        gates[name] = {'ok': p.returncode == 0, 'tail': (p.stdout + p.stderr).strip()[-600:]}
        print('%-13s %s' % (name, 'PASS' if p.returncode == 0 else 'FAIL'), flush=True)
    out = ROOT / 'results' / 'validation' / ('%s-scale.json' % o.lane)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'snapshot': str(snap.relative_to(ROOT)), 'build_s': times, 'gates': gates}, indent=1))
    sys.exit(0 if all(g['ok'] for g in gates.values()) else 1)


if __name__ == '__main__':
    main()
