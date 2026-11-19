"""Hidden checks for round 2 (`tinycoder batch`). Never in the bundle. Standard library only; run from the repo root.

  /usr/bin/python3 bench/hidden_scale.py <bin/tinycoder> [--only a,b] [--json validation/<id>.json]

A round-2 build is eligible for the ramp only if every check here passes AND Experiment 4's hidden checks
(bench/validate.py, 26 checks on serve/run) still pass.
"""
import argparse, json, shutil, subprocess, sys, tempfile, time, traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'ramp'))
import check_scale as C  # noqa: E402
from check_scale import Fail, need, recording, step, tu  # noqa: E402

TASKS = ROOT / 'bench' / 'tasks'
REC = ROOT / 'recordings'
NAMES = ['t1-bugfix', 't2-feature', 't3-failing']
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def hidden_ok(ws, task):
    w = Path(tempfile.mkdtemp(prefix='hs-')) / 'ws'
    shutil.copytree(ws, w, ignore=shutil.ignore_patterns('__pycache__'))
    for f in (TASKS / task / 'hidden').glob('*.py'):
        shutil.copy(f, w / f.name)
    ok = C.unit_ok(w)
    shutil.rmtree(w.parent, ignore_errors=True)
    return ok


@case
def h_real_tasks_300():
    """300 agents at once on the three real tasks (recorded Sonnet replies, 0.2x delays): all pass the hidden tests."""
    rp = C.Replay([REC], speed=0.2)
    try:
        agents = [('h%04d' % i, TASKS / NAMES[i % 3]) for i in range(300)]
        r = C.batch(agents, rp, 300, timeout=1800)
        need(r['code'] == 0, 'exit code %d (stderr %s)' % (r['code'], r['stderr'][-300:]))
        C.all_lines_ok(r)
        C.no_problems(rp)
        bad = [e['run_id'] for e, (_, t) in zip(r['entries'], agents) if not hidden_ok(e['workspace'], t.name)]
        need(not bad, '%d agents fail the hidden tests, e.g. %s' % (len(bad), bad[:3]))
    finally:
        rp.stop()


@case
def h_queue_1000_at_100():
    """1,000 agents with --concurrency 100 and no model delay: all finish; never over 100 calls in flight."""
    rp = C.Replay([REC], speed=0.0)
    try:
        r = C.batch([('q%04d' % i, TASKS / NAMES[i % 3]) for i in range(1000)], rp, 100, timeout=3600)
        need(r['code'] == 0, 'exit code %d' % r['code'])
        C.all_lines_ok(r)
        m = rp.stats()['max_in_flight']
        need(m <= 100, '%d calls in flight with --concurrency 100' % m)
    finally:
        rp.stop()


@case
def h_symlink_per_workspace():
    """A workspace holding a symlink into the next agent's workspace: reading through it is refused."""
    recs = Path(tempfile.mkdtemp(prefix='hs-recs-'))
    text = 'Hidden task (symlink). Read link/calc.py and reply.\n'
    recording(recs, 'sym', text, [step([tu('read_file', {'path': 'link/calc.py'})]),
                                  step([{'type': 'text', 'text': 'Done.'}], stop='end_turn')])
    t = C.make_task('practice-pass', text)
    rp = C.Replay([REC, recs, ROOT / 'tests' / 'recordings'], speed=0.1)
    try:
        base = Path(tempfile.mkdtemp(prefix='hs-sym-'))
        # Build the manifest by hand so the link can point at the neighbour's real workspace.
        entries = []
        for i in range(2):
            ws, runs = base / 'ws' / ('s%d' % i), base / 'runs' / ('s%d' % i)
            shutil.copytree((t if i == 0 else C.HERE / 'tasks' / 'practice-fail') / 'repo', ws)
            runs.mkdir(parents=True)
            entries.append({'run_id': 's%d' % i, 'task': str(t if i == 0 else C.HERE / 'tasks' / 'practice-fail'),
                            'workspace': str(ws), 'runs': str(runs)})
        (Path(entries[0]['workspace']) / 'link').symlink_to(entries[1]['workspace'])
        man = base / 'm.json'
        man.write_text(json.dumps({'agents': entries}))
        e = dict(C.env(), ANTHROPIC_BASE_URL='http://127.0.0.1:%d' % rp.port, ANTHROPIC_API_KEY='test-key')
        p = subprocess.run([C.BIN, 'batch', '--manifest', str(man), '--concurrency', '2'], env=e, capture_output=True, text=True, timeout=300)
        lines = {json.loads(l)['run_id']: json.loads(l) for l in p.stdout.splitlines() if l.strip()}
        ev = C.runs_events(entries[0])
        tools = [x for x in ev if x.get('type') == 'tool' and x.get('tool') == 'read_file']
        need(tools and not tools[0]['ok'], 's0: read_file through a symlink into another workspace was not refused')
        need(lines.get('s1', {}).get('status') == 'done', 's1 did not finish')
    finally:
        rp.stop()


@case
def h_step_limit_one_agent():
    """One agent never stops calling tools: it ends with step_limit after 20 steps; the others finish."""
    recs = Path(tempfile.mkdtemp(prefix='hs-recs-'))
    text = 'Hidden task (endless). Keep listing files.\n'
    recording(recs, 'endless', text, [step([tu('list_files', {})], delay=20) for _ in range(25)])
    t = C.make_task('practice-pass', text)
    rp = C.Replay([REC, recs], speed=1.0)
    try:
        agents = [('e%02d' % i, t if i == 0 else TASKS / NAMES[i % 3]) for i in range(9)]
        r = C.batch(agents, rp, 9, timeout=600)
        need(r['code'] == 0, 'exit code %d' % r['code'])
        C.all_lines_ok(r, statuses={'e00': 'step_limit'})
        need(r['lines']['e00']['steps'] == 20, 'e00: %s steps, expected 20' % r['lines']['e00']['steps'])
    finally:
        rp.stop()


@case
def h_slow_tool_one_agent():
    """One agent's command hits the 30 s timeout (exit_code -1); the others are not held up behind it."""
    recs = Path(tempfile.mkdtemp(prefix='hs-recs-'))
    text = 'Hidden task (slow command). Run the slow command and reply.\n'
    recording(recs, 'slow', text, [step([tu('run_command', {'argv': ['python3', '-c', 'import time; time.sleep(60)']})], delay=50),
                                   step([{'type': 'text', 'text': 'It timed out.'}], stop='end_turn', delay=50)])
    t = C.make_task('practice-pass', text)
    rp = C.Replay([REC, recs], speed=0.2)
    try:
        agents = [('w%02d' % i, t if i == 0 else TASKS / NAMES[i % 3]) for i in range(7)]
        t0 = time.time()
        r = C.batch(agents, rp, 7, timeout=600)
        need(r['code'] == 0, 'exit code %d' % r['code'])
        C.all_lines_ok(r)
        others = [r['lines'][rid]['wall_ms'] for rid, _ in agents[1:]]
        need(max(others) < 25000, 'other agents took up to %.0f ms: held up by the slow command' % max(others))
        need(r['lines']['w00']['wall_ms'] >= 29000, 'w00 finished too fast for a 30 s timeout')
    finally:
        rp.stop()


@case
def h_exp4_checks_still_pass():
    """Experiment 4's hidden checks on serve/run (bench/validate.py) still pass."""
    p = subprocess.run([sys.executable, str(ROOT / 'bench/validate.py'), C.BIN], capture_output=True, text=True, timeout=1800)
    need(p.returncode == 0, 'bench/validate.py failed: ' + (p.stdout + p.stderr).strip()[-400:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bin')
    ap.add_argument('--only')
    ap.add_argument('--json')
    o = ap.parse_args()
    C.BIN = str(Path(o.bin).resolve())
    only = set(o.only.split(',')) if o.only else None
    res = {}
    for fn in CASES:
        if only and fn.__name__ not in only:
            continue
        t0 = time.time()
        try:
            fn()
            res[fn.__name__] = 'PASS'
        except Fail as e:
            res[fn.__name__] = 'FAIL ' + str(e)
        except Exception:
            res[fn.__name__] = 'FAIL ' + traceback.format_exc().strip().splitlines()[-1][:300]
        print('%-28s %s  (%.0f s)' % (fn.__name__, res[fn.__name__], time.time() - t0), flush=True)
    if o.json:
        Path(o.json).parent.mkdir(parents=True, exist_ok=True)
        Path(o.json).write_text(json.dumps(res, indent=1))
    bad = [k for k, v in res.items() if not v.startswith('PASS')]
    print('ALL PASS' if not bad else '%d FAILED' % len(bad))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
