"""Round 2's Claude Code steps, one lane at a time: `scale` (add the batch command) then `optimize` (same session,
resumed), each recorded by tools/record_session.py exactly as in Experiment 4 (Opus 5.5, high effort, 30-minute cap,
sandbox, network filter, offline kit). Prints the plan and changes nothing unless --go. Run from the repo root.

  .venv/bin/python tools/run_scale_steps.py [--go] [--lanes py,ts,rs]

A step with an events.json already ran, so it is skipped and never rerun. A failed step stays failed: a repair round,
if Ranjan wants one, uses prompts/repair.txt from Experiment 4 and is written down in HISTORY.md.
"""
import argparse, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = [('scale', 'scale.txt', False), ('optimize', 'scale-optimize.txt', True)]


def techniques():
    rows = re.findall(r'^\| (\w+) \| ([^|]+) \| (.+) \|$', (ROOT / 'RULES.md').read_text(), re.M)
    return {k: '%s with %s' % (lang.strip(), allowed.strip().rstrip('.')) for k, lang, allowed in rows if k != 'ID'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--go', action='store_true')
    ap.add_argument('--lanes', default='py,ts,rs')
    o = ap.parse_args()
    tech = techniques()
    for lane in o.lanes.split(','):
        sid = lane + 's'
        bundle = ROOT / 'sessions/private' / ('bundle-%s' % sid)
        for step, pf, resume in STEPS:
            done = (ROOT / 'sessions/private' / sid / step / 'events.json').exists()
            prompt = (ROOT / 'prompts' / pf).read_text().strip().replace('{ID}', lane.upper()).replace('{TECHNIQUES}', tech[lane.upper()])
            pfile = ROOT / 'sessions/private' / sid / ('%s-prompt.txt' % step)
            print('%s %-8s %s' % (sid, step, 'already ran' if done else ('RUN' if o.go else 'would run')))
            print('    prompt:', prompt[:160] + '…')
            if done or not o.go:
                continue
            if not bundle.exists():
                sys.exit('no bundle %s: run tools/make_bundle_scale.py %s first' % (bundle, lane))
            pfile.parent.mkdir(parents=True, exist_ok=True)
            pfile.write_text(prompt + '\n')
            cmd = [sys.executable, str(ROOT / 'tools/record_session.py'), '--id', sid, '--step', step, '--prompt-file', str(pfile),
                   '--kit', lane, '--minutes', '30']
            cmd += ['--resume'] if resume else ['--bundle', str(bundle)]
            r = subprocess.run(cmd)
            if r.returncode:
                sys.exit('%s %s ended with exit %d; stopping here' % (sid, step, r.returncode))


if __name__ == '__main__':
    main()
