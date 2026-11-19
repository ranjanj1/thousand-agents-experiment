"""Visible checks for `tinycoder batch` (SPEC-SCALE.md). Standard library only, Python 3.9+.

  python3 tests/check_scale.py bin/tinycoder [--only name,name]

Uses the replay model (tests/replay_api.py) on one of ports 18401-18408 with the scripted recordings in
tests/recordings/. Each check prints PASS or FAIL with a reason. Exit code 0 only when every check passes.
Passing these is required but not enough: SPEC-SCALE.md section 5.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, traceback, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPLAY = HERE / 'replay_api.py' if (HERE / 'replay_api.py').exists() else ROOT / 'replay' / 'replay_api.py'
PORTS = list(range(18401, 18409))
CASES = []
BIN = None


class Fail(Exception):
    pass


def case(fn):
    CASES.append(fn)
    return fn


def need(cond, msg):
    if not cond:
        raise Fail(msg)


def env():
    e = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/tmp'), 'LANG': 'en_US.UTF-8'}
    for k in ('npm_config_cache', 'CARGO_HOME'):
        if k in os.environ:
            e[k] = os.environ[k]
    return e


class Replay:
    def __init__(self, rec_dirs, speed=1.0):
        self.dir = Path(tempfile.mkdtemp(prefix='tc-replay-'))
        self.log = self.dir / 'replay.jsonl'
        args = []
        for d in rec_dirs:
            args += ['--recordings', str(d)]
        for port in PORTS:
            self.p = subprocess.Popen([sys.executable, str(REPLAY)] + args + ['--log', str(self.log), '--port', str(port), '--speed', str(speed)],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            line = self.p.stdout.readline()
            if line.startswith('PORT'):
                self.port = port
                return
            self.p.wait()
        raise Fail('no free replay port in %d-%d' % (PORTS[0], PORTS[-1]))

    def stats(self):
        return json.load(urllib.request.urlopen('http://127.0.0.1:%d/stats' % self.port, timeout=5))

    def requests(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()] if self.log.exists() else []

    def stop(self):
        self.p.terminate()
        self.p.wait()


def make_task(name, text=None):
    """A copy of a practice task; with `text`, its TASK.md is replaced (so a special recording can answer it)."""
    src = HERE / 'tasks' / name
    d = Path(tempfile.mkdtemp(prefix='tc-stask-')) / name
    shutil.copytree(src, d)
    if text is not None:
        (d / 'TASK.md').write_text(text)
    return d


def recording(dirpath, name, task_text, steps):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / (name + '.json')).write_text(json.dumps({'source': name, 'task_text': task_text, 'steps': steps}))


def step(blocks, stop='tool_use', delay=200, **kw):
    return dict({'delay_ms': delay, 'stop_reason': stop, 'content': blocks, 'usage': {'input_tokens': 100, 'output_tokens': 20}}, **kw)


def tu(name, args):
    return {'type': 'tool_use', 'id': 'x', 'name': name, 'input': args}


def batch(agents, replay, concurrency, timeout=600):
    """agents: [(run_id, task_dir)]. Returns dict with code, lines {run_id: obj}, raw stdout, stderr, secs, entries."""
    base = Path(tempfile.mkdtemp(prefix='tc-batch-'))
    entries = []
    for rid, task in agents:
        ws, runs = base / 'ws' / rid, base / 'runs' / rid
        shutil.copytree(Path(task) / 'repo', ws)
        runs.mkdir(parents=True)
        entries.append({'run_id': rid, 'task': str(task), 'workspace': str(ws), 'runs': str(runs)})
    man = base / 'manifest.json'
    man.write_text(json.dumps({'agents': entries}))
    e = dict(env(), ANTHROPIC_BASE_URL='http://127.0.0.1:%d' % replay.port, ANTHROPIC_API_KEY='test-key')
    t0 = time.time()
    p = subprocess.run([BIN, 'batch', '--manifest', str(man), '--concurrency', str(concurrency)], env=e,
                       capture_output=True, text=True, timeout=timeout)
    lines, bad = {}, []
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            lines[d.get('run_id')] = d
        except ValueError:
            bad.append(line[:200])
    return {'code': p.returncode, 'lines': lines, 'bad': bad, 'stdout': p.stdout, 'stderr': p.stderr, 'secs': time.time() - t0,
            'entries': entries, 'base': base}


def runs_events(entry):
    f = Path(entry['runs']) / (entry['run_id'] + '.jsonl')
    need(f.exists(), '%s: runs file %s.jsonl not written' % (entry['run_id'], entry['run_id']))
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def unit_ok(ws):
    p = subprocess.run(['python3', '-m', 'unittest', '-q'], cwd=ws, capture_output=True, text=True, timeout=120, env=env())
    return p.returncode == 0


def all_lines_ok(r, statuses=None):
    need(not r['bad'], 'stdout has lines that are not JSON: %r' % r['bad'][:2])
    need(len(r['stdout'].strip().splitlines()) == len(r['entries']),
         'expected %d stdout lines (one per agent), got %d' % (len(r['entries']), len(r['stdout'].strip().splitlines())))
    for e in r['entries']:
        d = r['lines'].get(e['run_id'])
        need(d is not None, 'no stdout line for %s (stderr: %s)' % (e['run_id'], r['stderr'][-300:]))
        for k in ('status', 'reason', 'steps', 'tool_calls', 'blocked_calls', 'input_tokens', 'output_tokens',
                  'wall_ms', 'llm_ms', 'tool_ms', 'final_text'):
            need(k in d, '%s: final object is missing %s' % (e['run_id'], k))
        want = (statuses or {}).get(e['run_id'], 'done')
        need(d['status'] == want, '%s: status %r, expected %r' % (e['run_id'], d['status'], want))
        ev = runs_events(e)
        need(ev and ev[-1].get('type') == 'final', '%s: last runs line must have type "final"' % e['run_id'])
        need({k: v for k, v in ev[-1].items() if k != 'type'} == {k: v for k, v in d.items() if k != 'run_id'},
             '%s: last runs line must equal the stdout object without run_id, plus "type"' % e['run_id'])
        if want != 'error':   # a run that ends on an API error may have had no successful model call
            need(sum(1 for x in ev if x.get('type') == 'llm') >= 1, '%s: no llm lines in the runs file' % e['run_id'])


def no_problems(rp):
    for q in rp.requests():
        need(not q['problems'], 'model request %d (agent %s step %s): %s' % (q['n'], q['slot'], q['step'], '; '.join(q['problems'])))


# ---------- checks ----------

@case
def batch_basic():
    """12 agents, two tasks, concurrency 4: every agent done, files right, fixes really made."""
    rp = Replay([HERE / 'recordings'], speed=0.2)
    try:
        agents = [('a%02d' % i, HERE / 'tasks' / ('practice-fail' if i % 2 else 'practice-pass')) for i in range(12)]
        r = batch(agents, rp, 4)
        need(r['code'] == 0, 'exit code %d, expected 0 (stderr: %s)' % (r['code'], r['stderr'][-300:]))
        all_lines_ok(r)
        no_problems(rp)
        for e, (rid, task) in zip(r['entries'], agents):
            need(unit_ok(e['workspace']), '%s: the task tests fail in its workspace after the run' % rid)
        need(rp.stats()['agents'] == 12, 'replay saw %d agents, expected 12' % rp.stats()['agents'])
    finally:
        rp.stop()


@case
def batch_concurrency_cap():
    """30 agents with --concurrency 6: never more than 6 model calls at once, and really in parallel."""
    rp = Replay([HERE / 'recordings'], speed=0.5)
    try:
        r = batch([('c%02d' % i, HERE / 'tasks' / 'practice-pass') for i in range(30)], rp, 6)
        need(r['code'] == 0, 'exit code %d' % r['code'])
        all_lines_ok(r)
        m = rp.stats()['max_in_flight']
        need(m <= 6, 'up to %d model calls in flight; --concurrency was 6' % m)
        need(m >= 3, 'at most %d model call in flight at once: agents are not running in parallel' % m)
    finally:
        rp.stop()


@case
def batch_runs_in_parallel():
    """20 agents at once on a 2.1 s recording finish in far less than 20 x 2.1 s."""
    rp = Replay([HERE / 'recordings'], speed=1.0)
    try:
        r = batch([('p%02d' % i, HERE / 'tasks' / 'practice-pass') for i in range(20)], rp, 20)
        need(r['code'] == 0, 'exit code %d' % r['code'])
        all_lines_ok(r)
        need(r['secs'] < 15, 'batch took %.1f s; 20 agents in parallel should take a few seconds' % r['secs'])
    finally:
        rp.stop()


@case
def batch_isolation():
    """An agent asked to read another agent's file is blocked; workspaces never mix."""
    recs = Path(tempfile.mkdtemp(prefix='tc-recs-'))
    text = 'Practice task (isolation). Read the other copy of calc.py and report it.\n'
    recording(recs, 'peek', text, [step([tu('read_file', {'path': '../b01/calc.py'}), tu('list_files', {'path': '..'})]),
                                   step([{'type': 'text', 'text': 'Done.'}], stop='end_turn')])
    peek = make_task('practice-pass', text)
    rp = Replay([HERE / 'recordings', recs], speed=0.1)
    try:
        r = batch([('b00', peek), ('b01', HERE / 'tasks' / 'practice-fail'), ('b02', peek)], rp, 3)
        need(r['code'] == 0, 'exit code %d' % r['code'])
        all_lines_ok(r)
        for rid in ('b00', 'b02'):
            need(r['lines'][rid]['blocked_calls'] >= 1, '%s: reading ../b01/calc.py was not blocked by the hook' % rid)
        need(unit_ok(r['entries'][1]['workspace']), 'b01: its own fix is missing')
        for i in (0, 2):
            need((Path(r['entries'][i]['workspace']) / 'calc.py').read_text() == (peek / 'repo' / 'calc.py').read_text(),
                 '%s: its calc.py changed (another agent wrote into its workspace)' % r['entries'][i]['run_id'])
    finally:
        rp.stop()


@case
def batch_failure_stays_local():
    """One agent gets a permanent API error; it ends with status error, every other agent finishes."""
    recs = Path(tempfile.mkdtemp(prefix='tc-recs-'))
    text = 'Practice task (broken API). The tests already pass. Run them and reply with one sentence.\n'
    recording(recs, 'broken', text, [{'delay_ms': 100, 'status': 400}])
    bad = make_task('practice-pass', text)
    rp = Replay([HERE / 'recordings', recs], speed=0.2)
    try:
        agents = [('f%02d' % i, bad if i == 3 else HERE / 'tasks' / 'practice-fail') for i in range(8)]
        r = batch(agents, rp, 8)
        need(r['code'] == 0, 'exit code %d, expected 0: one agent failing must not fail the batch' % r['code'])
        all_lines_ok(r, statuses={'f03': 'error'})
    finally:
        rp.stop()


@case
def batch_retry_per_agent():
    """A 529 then a 200 on one agent's second call: retried as SPEC 4.1 says, and not counted as a step."""
    recs = Path(tempfile.mkdtemp(prefix='tc-recs-'))
    text = 'Practice task (overloaded once). The tests already pass. Run them and reply with one sentence.\n'
    # The 529 is on the second call: the replay model can only tell a retry from a new agent once the agent has a reply.
    recording(recs, 'flaky', text, [step([tu('run_tests', {})]),
                                    step([{'type': 'text', 'text': 'They pass.'}], stop='end_turn', fail_first=[529])])
    flaky = make_task('practice-pass', text)
    rp = Replay([HERE / 'recordings', recs], speed=0.1)
    try:
        r = batch([('r00', flaky), ('r01', HERE / 'tasks' / 'practice-pass')], rp, 2)
        need(r['code'] == 0, 'exit code %d' % r['code'])
        all_lines_ok(r)
        need(r['lines']['r00']['steps'] == 2, 'r00: %d steps, expected 2 (a retry is not a step)' % r['lines']['r00']['steps'])
    finally:
        rp.stop()


@case
def batch_bad_manifest():
    """A manifest that is not JSON: exit code 1 and nothing on stdout."""
    d = Path(tempfile.mkdtemp(prefix='tc-badman-'))
    (d / 'm.json').write_text('{not json')
    p = subprocess.run([BIN, 'batch', '--manifest', str(d / 'm.json'), '--concurrency', '2'], env=env(),
                       capture_output=True, text=True, timeout=60)
    need(p.returncode == 1, 'exit code %d, expected 1' % p.returncode)
    need(not p.stdout.strip(), 'stdout must be empty, got %r' % p.stdout[:200])


def main(names=None):
    global BIN
    ap = argparse.ArgumentParser()
    ap.add_argument('bin')
    ap.add_argument('--only')
    o = ap.parse_args()
    BIN = str(Path(o.bin).resolve())
    only = set(o.only.split(',')) if o.only else None
    failed = 0
    for fn in CASES:
        if only and fn.__name__ not in only:
            continue
        t0 = time.time()
        try:
            fn()
            print('PASS %-26s %.1f s' % (fn.__name__, time.time() - t0), flush=True)
        except Fail as e:
            failed += 1
            print('FAIL %-26s %s' % (fn.__name__, e), flush=True)
        except Exception:
            failed += 1
            print('FAIL %-26s %s' % (fn.__name__, traceback.format_exc().strip().splitlines()[-1][:300]), flush=True)
    print('%d failed' % failed if failed else 'ALL PASS')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
