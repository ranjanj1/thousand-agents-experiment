"""Builds results/summary.json, the one data file the film reads, from every step record. Run from the repo root.

  /usr/bin/python3 ramp/summarize.py

For each round, language and number of agents: how many sittings ran it, how many passed, and the median across
sittings of: batch time, time outside the model per agent (median and p95), extra wait per model call, peak
memory and memory per agent, peak processes, swap growth, CPU. The wall per language and round is the smallest
number of agents that failed in at least half of the sittings, with how it failed. Dry runs and live steps are kept apart.
"""
import json, statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def main():
    recs = []
    for f in sorted((ROOT / 'results/ramp').glob('*/*.json')):
        if f.name.startswith(('sitting', 's1-', 's2-', 's3-')) or f.parent.name.startswith('dry'):
            continue
        r = json.loads(f.read_text())
        if 'lane' in r:
            recs.append(r)
    out = {'note': 'Every number is a median across sittings of the step records in results/ramp/. Replay model unless api=live.',
           'steps': {}, 'walls': {}, 'live': []}
    groups = {}
    for r in recs:
        if r['api'] == 'live':
            out['live'].append({k: r[k] for k in ('id', 'sitting', 'lane', 'round', 'n', 'outcome', 'ok', 'agents', 'time', 'memory')}
                               | {'cost_usd': r['model'].get('live', {}).get('cost_usd')})
            continue
        groups.setdefault((r['round'], r['lane'], r['n']), []).append(r)
    for (rnd, lane, n), rs in sorted(groups.items()):
        out['steps'].setdefault('round%d' % rnd, {}).setdefault(lane, {})[str(n)] = {
            'sittings': len(rs), 'passed': sum(1 for r in rs if r['ok']),
            'outcomes': sorted({r['outcome'] for r in rs}),
            'hidden_pass': [r['agents']['hidden_pass'] for r in rs],
            'batch_wall_s': med([r['time']['batch_wall_s'] for r in rs]),
            'outside_model_ms_median': med([r['time']['outside_model_ms']['median'] for r in rs]),
            'outside_model_ms_p95': med([r['time']['outside_model_ms']['p95'] for r in rs]),
            'extra_wait_per_call_ms': med([r['model']['extra_wait_per_call_ms'] for r in rs]),
            'peak_footprint_mb': med([r['memory']['peak_footprint_mb'] for r in rs]),
            'per_agent_footprint_mb': med([r['memory']['per_agent_footprint_mb'] for r in rs]),
            'peak_processes': med([r['memory']['peak_processes'] for r in rs]),
            'swap_growth_mb': med([r['memory']['swap_growth_mb'] for r in rs]),
            'peak_pressure': max(r['memory']['peak_pressure'] or 0 for r in rs),
            'cpu_avg_pct': med([r['cpu']['avg_pct'] for r in rs]),
            'spawn_errors': [r['agents']['spawn_errors'] for r in rs if r['agents']['spawn_errors']],
        }
    for rnd, lanes in out['steps'].items():
        for lane, by_n in lanes.items():
            for n in sorted(by_n, key=int):
                s = by_n[n]
                failed = s['sittings'] - s['passed']
                if failed and failed >= s['passed']:   # failed in at least half of the sittings
                    out['walls'].setdefault(rnd, {})[lane] = {'agents': int(n), 'how': s['outcomes']}
                    break
            else:
                out['walls'].setdefault(rnd, {})[lane] = None
    (ROOT / 'results/summary.json').write_text(json.dumps(out, indent=1))
    for rnd, lanes in out['steps'].items():
        print(rnd)
        for lane, by_n in lanes.items():
            for n in sorted(by_n, key=int):
                s = by_n[n]
                print('  %s %5s agents: %d/%d sittings ok, batch %s s, outside model %s ms (p95 %s), %s MB peak (%s MB/agent), swap +%s MB' % (
                    lane, n, s['passed'], s['sittings'], s['batch_wall_s'], s['outside_model_ms_median'], s['outside_model_ms_p95'],
                    s['peak_footprint_mb'], s['per_agent_footprint_mb'], s['swap_growth_mb']))
    print('walls:', json.dumps(out['walls']))


if __name__ == '__main__':
    main()
