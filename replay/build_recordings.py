"""Turns Experiment 4's 45 real agent runs into replay recordings. Standard library only; run from the repo root.

  /usr/bin/python3 replay/build_recordings.py [--exp4 ../tinycoder-experiment] [--dry]

Source: <exp4>/sessions/private/agent-runs/<lane>-<task>-<n>/calls.jsonl, written by Experiment 4's relay: every
request and response in full, plus `ms`, the time the relay waited for Anthropic. Only HTTP 200 exchanges become steps
(a retried error is not a model step). Each recording keeps the reply content unchanged, the stop reason, the four
token counts and the measured delay. A run is used only if Experiment 4 recorded it as passing the hidden tests.

Writes recordings/<task>/<lane>-<task>-<n>.json and recordings/INDEX.json (counts, steps, delays). --dry checks only.
"""
import argparse, hashlib, json, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


def text_of(content):
    return content if isinstance(content, str) else ''.join(b.get('text', '') for b in content if b.get('type') == 'text')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exp4', default=str(ROOT.parent / 'tinycoder-experiment'))
    ap.add_argument('--dry', action='store_true')
    o = ap.parse_args()
    e4 = Path(o.exp4)
    runs = sorted(d for d in (e4 / 'sessions/private/agent-runs').iterdir() if d.is_dir() and d.name[:3] in ('py-', 'ts-', 'rs-'))
    out, index, bad = ROOT / 'recordings', {'source': 'Experiment 4 agent runs (Sonnet 5.5 via OpenRouter, provider Anthropic)', 'runs': []}, []
    for d in runs:
        rid = d.name
        lane, rest = rid.split('-', 1)
        task, n = rest.rsplit('-', 1)
        calls = [json.loads(l) for l in (d / 'calls.jsonl').read_text().splitlines() if l.strip()]
        ok = [c for c in calls if c['status'] == 200]
        res = json.loads((e4 / 'results/agent' / (rid + '.json')).read_text())
        if not res.get('hidden_tests_pass'):
            bad.append('%s: did not pass hidden tests in Exp 4' % rid)
            continue
        steps = []
        for i, c in enumerate(ok):
            r = c['response']
            m = c['request']['messages']
            if len(m) != 2 * i + 1:
                bad.append('%s step %d: request has %d messages, expected %d' % (rid, i, len(m), 2 * i + 1))
            steps.append({'delay_ms': round(c['ms'], 1), 'stop_reason': r['stop_reason'], 'content': r['content'],
                          'usage': {k: (r.get('usage') or {}).get(k) or 0 for k in TOKENS}})
        if steps[-1]['stop_reason'] != 'end_turn':
            bad.append('%s: last reply is %s, not end_turn' % (rid, steps[-1]['stop_reason']))
        task_text = text_of(ok[0]['request']['messages'][0]['content'])
        exp_text = (e4 / 'bench/tasks' / task / 'TASK.md').read_text()
        if task_text != exp_text:
            bad.append('%s: first user message is not the exact TASK.md' % rid)
        rec = {'source': rid, 'lane_recorded': lane, 'task': task, 'task_text': task_text, 'steps': steps,
               'retries_dropped': len(calls) - len(ok)}
        d_ms = [s['delay_ms'] for s in steps]
        index['runs'].append({'id': rid, 'task': task, 'steps': len(steps), 'delay_ms_total': round(sum(d_ms), 1),
                              'tool_calls': sum(1 for s in steps for b in s['content'] if b.get('type') == 'tool_use'),
                              'retries_dropped': rec['retries_dropped']})
        if not o.dry:
            f = out / task / (rid + '.json')
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(rec, indent=1))
    by_task = {}
    for r in index['runs']:
        by_task.setdefault(r['task'], []).append(r)
    index['by_task'] = {t: {'recordings': len(v), 'steps_median': statistics.median(x['steps'] for x in v),
                            'delay_s_median': round(statistics.median(x['delay_ms_total'] for x in v) / 1000, 2)}
                        for t, v in sorted(by_task.items())}
    index['problems'] = bad
    for t, v in index['by_task'].items():
        print('%-11s %2d recordings, median %s steps, median %.2f s of model time' % (t, v['recordings'], v['steps_median'], v['delay_s_median']))
    print('%d recordings, %d problems' % (len(index['runs']), len(bad)))
    for b in bad:
        print('  PROBLEM', b)
    if not o.dry:
        (out / 'INDEX.json').write_text(json.dumps(index, indent=1))
        h = hashlib.sha256(b''.join(f.read_bytes() for f in sorted(out.rglob('*.json')))).hexdigest()
        print('wrote', out, 'sha256 of all files', h[:16])
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
