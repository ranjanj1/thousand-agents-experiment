"""One official sitting: the whole ramp for every language, replay model, no money spent. Run from the repo root on a
freshly restarted Mac on AC power, nothing else open.

  caffeinate -i /usr/bin/python3 ramp/sitting.py <1|2|3> --round 1 [--levels 10,25,50,100,250,500,1000] [--speed 1.0]

Order: levels from small to large; at each level the three languages in a new shuffled order (fixed seed per
sitting). A language stops climbing in this sitting after its first failed step (not every agent passed, or the
step was stopped by a guard); the failure is the result. Between steps it waits until memory pressure is normal and
then 20 s more. Each step is ramp/ramp.py, so every step's files are in results/ramp/s<k>/.
"""
import argparse, json, random, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ramp'))
import measure_lib as M  # noqa: E402

SEEDS = {1: 20261121, 2: 20261122, 3: 20261123}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sitting', type=int, choices=[1, 2, 3])
    ap.add_argument('--round', type=int, required=True, choices=[1, 2])
    ap.add_argument('--levels', default='10,25,50,100,250,500,1000')
    ap.add_argument('--speed', default='1.0')
    ap.add_argument('--lanes', default='py,ts,rs')
    o = ap.parse_args()
    name = 's%d' % o.sitting
    if subprocess.run([sys.executable, str(ROOT / 'ramp/preflight.py'), '--strict', '--json',
                       str(ROOT / ('results/ramp/%s-r%d-preflight.json' % (name, o.round)))]).returncode != 0:
        sys.exit('preflight refused; not starting')
    rnd = random.Random(SEEDS[o.sitting] * 10 + o.round)
    levels = [int(x) for x in o.levels.split(',')]
    lanes = o.lanes.split(',')
    walled, log = {}, []
    for n in levels:
        order = lanes[:]
        rnd.shuffle(order)
        for lane in order:
            if lane in walled:
                continue
            while M.pressure_level() not in (1, None):
                time.sleep(5)
            time.sleep(20)
            t = time.time()
            r = subprocess.run([sys.executable, str(ROOT / 'ramp/ramp.py'), '--lane', lane, '--round', str(o.round), '--n', str(n),
                                '--sitting', name, '--speed', o.speed])
            rec_f = ROOT / 'results/ramp' / name / ('%s%d-n%d.json' % (lane, o.round, n))
            rec = json.loads(rec_f.read_text()) if rec_f.exists() else {'ok': False, 'outcome': 'no record (exit %d)' % r.returncode}
            log.append({'lane': lane, 'n': n, 'ok': rec['ok'], 'outcome': rec['outcome'], 'secs': round(time.time() - t)})
            if not rec['ok']:
                walled[lane] = n
                print('== %s stops climbing at %d agents: %s' % (lane, n, rec['outcome']), flush=True)
    (ROOT / 'results/ramp' / name / ('sitting-r%d.json' % o.round)).write_text(json.dumps({'steps': log, 'wall': walled}, indent=1))
    print('SITTING %s ROUND %d FINISHED %s; walls: %s' % (name, o.round, time.ctime(), walled or 'none'))


if __name__ == '__main__':
    main()
