"""One step of the ramp: N tinycoder agents of one language started at the same moment, measured from outside.
Standard library only; run with /usr/bin/python3 from the repo root, outside the sandbox.

  /usr/bin/python3 ramp/ramp.py --lane py --round 1 --n 100 --sitting s1 [--speed 1.0] [--api replay|live]
                                [--concurrency N] [--timeout-min 20] [--swap-limit-mb 2048] [--keep]

Round 1 starts N copies of Experiment 4's `tinycoder run` (each starts its own two MCP servers). Round 2 starts one
`tinycoder batch` process with a manifest of N agents and --concurrency (default N). Agent i works on task
t1/t2/t3 by i mod 3, in a fresh copy of that task's repo. Workspaces are made before the clock starts.

The model is the replay model (replay/replay_api.py) unless --api live, which goes to Claude Sonnet 5.5 through
Experiment 4's OpenRouter relay and needs --budget (dollars) plus enough credit. Live calls cost money: RUN.md says
when; ask Ranjan first.

Measured every 0.5 s (ramp/measure_lib.py): memory (footprint, as Activity Monitor shows it, and resident) and CPU
of every process the step started (the replay model excluded), number of processes, swap, compressed memory,
memory pressure. The step is stopped (and every agent killed) when swap grows by more than --swap-limit-mb, when
pressure stays critical for 10 s, or at the timeout. After it ends, every finished workspace gets its task's own tests
and the hidden tests (not timed).

Writes results/ramp/<sitting>/<lane><round>-n<N>.json (the record), .agents.jsonl (one line per agent),
.samples.jsonl and .replay.jsonl. A step that already has a record is never repeated (sittings named dry* excepted).
"""
import argparse, json, os, shutil, signal, statistics, subprocess, sys, tempfile, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ramp'))
import measure_lib as M  # noqa: E402
import preflight  # noqa: E402
from builds import BUILDS, RUN_PATH  # noqa: E402

TASKS = ['t1-bugfix', 't2-feature', 't3-failing']
TASK_DIR = ROOT / 'bench' / 'tasks'
RELAY_PORT = 18480
PRESSURE_CRITICAL_S = 10


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def start_server(argv, what):
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = p.stdout.readline()
    if not (line.startswith('PORT') or line.startswith('RELAY')):
        p.kill()
        sys.exit('%s did not start: %r' % (what, line))
    port = int(line.split()[1]) if line.startswith('PORT') else RELAY_PORT
    threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()   # drain
    return p, port


def kill_tree(exclude):
    table = M.process_table()
    excl = set(exclude)
    for e in list(excl):
        excl.update(M.descendants(table, e))
    for pid in M.descendants(table, os.getpid()):
        if pid not in excl:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def unit_tests(ws, task, hidden):
    """The task's own tests (hidden=False) or own + hidden tests (hidden=True), on a copy of the workspace."""
    w = Path(tempfile.mkdtemp(prefix='ta-chk-')) / 'ws'
    try:
        shutil.copytree(ws, w, ignore=shutil.ignore_patterns('__pycache__'))
        if hidden:
            for f in sorted((TASK_DIR / task / 'hidden').glob('*.py')):
                shutil.copy(f, w / f.name)
        p = subprocess.run(['python3', '-m', 'unittest', '-q'], cwd=w, capture_output=True, text=True, timeout=120,
                           env={'PATH': RUN_PATH, 'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8'})
        return p.returncode == 0
    except Exception:
        return False
    finally:
        shutil.rmtree(w.parent, ignore_errors=True)


def credit():
    sys.path.insert(0, str(ROOT / 'live'))
    from live_lib import credit as c
    return c()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lane', required=True, choices=['py', 'ts', 'rs'])
    ap.add_argument('--round', type=int, required=True, choices=[1, 2])
    ap.add_argument('--n', type=int, required=True)
    ap.add_argument('--sitting', required=True)
    ap.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--api', choices=['replay', 'live'], default='replay')
    ap.add_argument('--budget', type=float, help='live only: most this step may spend, in dollars')
    ap.add_argument('--concurrency', type=int, help='round 2 only (default N)')
    ap.add_argument('--timeout-min', type=float, default=20)
    ap.add_argument('--swap-limit-mb', type=float, default=2048)
    ap.add_argument('--keep', action='store_true', help='keep the workspaces')
    o = ap.parse_args()

    b = BUILDS[(o.lane, o.round)]
    binp = b['dir'] / 'bin' / 'tinycoder'
    if not binp.exists():
        sys.exit('no build at %s' % binp)
    sid = '%s%d-n%d%s' % (o.lane, o.round, o.n, '-live' if o.api == 'live' else '')
    out = ROOT / 'results' / 'ramp' / o.sitting
    out.mkdir(parents=True, exist_ok=True)
    rec_f = out / (sid + '.json')
    if rec_f.exists() and not o.sitting.startswith('dry'):
        sys.exit('%s/%s already ran; not repeating' % (o.sitting, sid))
    for suffix in ('.agents.jsonl', '.samples.jsonl', '.replay.jsonl'):
        (out / (sid + suffix)).unlink(missing_ok=True)   # only reached for dry sittings

    if o.api == 'live':
        if o.budget is None:
            sys.exit('--api live needs --budget (dollars); ask Ranjan first')
        left = credit()
        print('OpenRouter credit before: $%.4f' % left, flush=True)
        if left < o.budget + 1.5:
            sys.exit('credit $%.2f is below budget $%.2f + $1.50 margin; not starting' % (left, o.budget))

    pre = preflight.facts()
    print('%s: %s, %d agents, %s model at speed %s. Swap %s MB, pressure %s, %d processes of mine.' % (
        sid, b['what'], o.n, o.api, o.speed, pre['swap_used_mb'], pre['pressure'], pre['processes_mine']), flush=True)

    # ---- workspaces (before the clock) ----
    base = Path(tempfile.mkdtemp(prefix='ta-%s-' % sid))
    agents = []
    for i in range(o.n):
        task = TASKS[i % 3]
        ws, runs = base / 'ws' / ('%04d' % i), base / 'runs' / ('%04d' % i)
        shutil.copytree(TASK_DIR / task / 'repo', ws)
        runs.mkdir(parents=True)
        agents.append({'i': i, 'run_id': '%s-%04d' % (sid, i), 'task': task, 'ws': ws, 'runs': runs})

    # ---- the model ----
    if o.api == 'replay':
        server, port = start_server([sys.executable, '-I', str(ROOT / 'replay/replay_api.py'), '--recordings', str(ROOT / 'recordings'),
                                     '--log', str(out / (sid + '.replay.jsonl')), '--speed', str(o.speed)], 'replay model')
        key = 'test-key'
    else:
        logd = out / (sid + '.relay')
        logd.mkdir(exist_ok=True)
        server, port = start_server([sys.executable, '-I', str(ROOT / 'tools/openrouter_relay.py'), '--port', str(RELAY_PORT),
                                     '--log', str(logd)], 'OpenRouter relay')
        key = 'relay-dummy'
    url = 'http://127.0.0.1:%d' % port

    def env(rid=None):
        e = {'PATH': RUN_PATH, 'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8', 'ANTHROPIC_BASE_URL': url, 'ANTHROPIC_API_KEY': key}
        if rid:
            e['TINYCODER_RUN_ID'] = rid
        return e

    # ---- sampling ----
    samples_f = open(out / (sid + '.samples.jsonl'), 'w', buffering=1)
    sampler = M.Sampler(os.getpid(), lambda s: samples_f.write(json.dumps(s) + '\n'), every=0.5, exclude=[server.pid])
    base_sys = M.system_snapshot()
    sampler.one()
    sampler.start()

    # ---- launch ----
    procs, spawn_errors = [], {}
    t0 = time.time()
    if b['how'] == 'run':
        for a in agents:
            so, se = open(a['runs'] / 'stdout.txt', 'w'), open(a['runs'] / 'stderr.txt', 'w')
            try:
                p = subprocess.Popen([str(binp), 'run', '--task', str(TASK_DIR / a['task']), '--workspace', str(a['ws']),
                                      '--runs', str(a['runs'])], env=env(a['run_id']), stdout=so, stderr=se, stdin=subprocess.DEVNULL)
                a['pid'], a['t_launch'] = p.pid, time.time() - t0
                procs.append((a, p))
            except OSError as e:
                a['spawn_error'] = '%s (errno %s)' % (e.strerror, e.errno)
                spawn_errors[a['spawn_error']] = spawn_errors.get(a['spawn_error'], 0) + 1
            finally:
                so.close()
                se.close()
    else:
        conc = o.concurrency or o.n
        man = base / 'manifest.json'
        man.write_text(json.dumps({'agents': [{'run_id': a['run_id'], 'task': str(TASK_DIR / a['task']), 'workspace': str(a['ws']),
                                               'runs': str(a['runs'])} for a in agents]}))
        so, se = open(base / 'batch-stdout.txt', 'w'), open(base / 'batch-stderr.txt', 'w')
        p = subprocess.Popen([str(binp), 'batch', '--manifest', str(man), '--concurrency', str(conc)], env=env(),
                             stdout=so, stderr=se, stdin=subprocess.DEVNULL)
        so.close()
        se.close()
        procs.append(({'batch': True}, p))
        for a in agents:
            a['t_launch'] = 0.0
    t_launched = time.time() - t0
    print('launched in %.2f s; %d spawn errors' % (t_launched, sum(spawn_errors.values())), flush=True)

    # ---- wait, with the guards ----
    outcome, crit_since, alive = 'complete', None, list(procs)
    last_print = 0
    while alive:
        time.sleep(0.1)
        now = time.time() - t0
        still = []
        for a, p in alive:
            if p.poll() is None:
                still.append((a, p))
            else:
                a['t_exit'], a['exit_code'] = now, p.returncode
        alive = still
        s = sampler.last or {}
        if s.get('swap_used_mb') is not None and base_sys['swap_used_mb'] is not None and \
                s['swap_used_mb'] - base_sys['swap_used_mb'] > o.swap_limit_mb:
            outcome = 'stopped: swap grew over %d MB' % o.swap_limit_mb
        if s.get('pressure') == 4:
            crit_since = crit_since or now
            if now - crit_since >= PRESSURE_CRITICAL_S:
                outcome = 'stopped: memory pressure critical for %d s' % PRESSURE_CRITICAL_S
        else:
            crit_since = None
        if now > o.timeout_min * 60:
            outcome = 'stopped: timeout %g min' % o.timeout_min
        if outcome != 'complete':
            print(outcome, flush=True)
            kill_tree([server.pid])
            time.sleep(1)
            for a, p in alive:
                p.poll()
                a['t_exit'], a['exit_code'], a['killed'] = now, p.returncode, True
            break
        if now - last_print > 10:
            last_print = now
            print('  %5.0f s: %d running, footprint %s MB, %s processes, swap %s MB, pressure %s' % (
                now, len(alive), s.get('footprint_mb'), s.get('processes'), s.get('swap_used_mb'), s.get('pressure')), flush=True)
    t_end = max([a.get('t_exit', 0) for a, _ in procs] or [0])
    time.sleep(1.0)  # one more sample after the end
    sampler.stop()
    samples_f.close()

    try:
        rstats = json.load(urllib.request.urlopen(url + '/stats', timeout=5)) if o.api == 'replay' else None
    except Exception as e:
        rstats = {'error': repr(e)[:200]}
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()

    # ---- per-agent results ----
    if b['how'] == 'batch':
        a0, p = procs[0]
        lines = {}
        for line in (base / 'batch-stdout.txt').read_text(errors='replace').splitlines():
            try:
                d = json.loads(line)
                lines[d.get('run_id')] = d
            except ValueError:
                pass
        for a in agents:
            a['final'] = lines.get(a['run_id'])
            a['exit_code'] = a0.get('exit_code')
        batch_stderr_tail = (base / 'batch-stderr.txt').read_text(errors='replace')[-3000:]
    else:
        batch_stderr_tail = None
        for a in agents:
            try:
                a['final'] = json.loads((a['runs'] / 'stdout.txt').read_text().strip().splitlines()[-1])
            except (ValueError, IndexError, OSError):
                a['final'] = None
    for a in agents:
        rf = a['runs'] / (a['run_id'] + '.jsonl')
        ev = []
        if rf.exists():
            for line in rf.read_text(errors='replace').splitlines():
                try:
                    ev.append(json.loads(line))
                except ValueError:
                    pass
        a['llm_calls'] = sum(1 for e in ev if e.get('type') == 'llm')
        a['runs_final_matches'] = bool(ev) and ev[-1].get('type') == 'final' and a['final'] is not None and \
            {k: v for k, v in ev[-1].items() if k != 'type'} == {k: v for k, v in a['final'].items() if k != 'run_id'}
        if a.get('final') is None and b['how'] == 'run':
            a['stderr_tail'] = (a['runs'] / 'stderr.txt').read_text(errors='replace')[-400:] if (a['runs'] / 'stderr.txt').exists() else ''

    done = [a for a in agents if a.get('final') and a['final'].get('status') == 'done']
    print('checking %d finished workspaces (not timed)...' % len(done), flush=True)
    with ThreadPoolExecutor(6) as ex:
        vis = list(ex.map(lambda a: unit_tests(a['ws'], a['task'], False), done))
        hid = list(ex.map(lambda a: unit_tests(a['ws'], a['task'], True), done))
    for a, v, h in zip(done, vis, hid):
        a['visible_pass'], a['hidden_pass'] = v, h

    F = lambda a, k: (a.get('final') or {}).get(k)
    outside = [F(a, 'wall_ms') - F(a, 'llm_ms') for a in done if F(a, 'wall_ms') is not None and F(a, 'llm_ms') is not None]
    walls = [F(a, 'wall_ms') for a in done]
    llm_total = sum(F(a, 'llm_ms') or 0 for a in agents if a.get('final'))
    calls = sum(a['llm_calls'] for a in agents)
    statuses = {}
    for a in agents:
        k = 'spawn_failed' if a.get('spawn_error') else ('killed' if a.get('killed') else (F(a, 'status') or 'no_output'))
        statuses[k] = statuses.get(k, 0) + 1
    passed = sum(1 for a in agents if a.get('hidden_pass'))
    ru = __import__('resource').getrusage(__import__('resource').RUSAGE_CHILDREN)
    peak = sampler.peak
    rec = {
        'id': sid, 'sitting': o.sitting, 'lane': o.lane, 'round': o.round, 'n': o.n, 'build': str(b['dir']), 'build_what': b['what'],
        'api': o.api, 'speed': o.speed, 'concurrency': (o.concurrency or o.n) if b['how'] == 'batch' else o.n,
        'started': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(t0)), 'outcome': outcome,
        'ok': outcome == 'complete' and passed == o.n,
        'agents': {'finished_done': len(done), 'hidden_pass': passed, 'visible_pass': sum(1 for a in agents if a.get('visible_pass')),
                   'statuses': statuses, 'spawn_errors': spawn_errors,
                   'runs_file_matches_stdout': sum(1 for a in agents if a['runs_final_matches'])},
        'time': {'launch_s': round(t_launched, 3), 'batch_wall_s': round(t_end, 2),
                 'agent_wall_ms': {'median': med(walls), 'p95': pct(walls, 0.95), 'max': max(walls) if walls else None},
                 'outside_model_ms': {'median': med(outside), 'p95': pct(outside, 0.95), 'max': max(outside) if outside else None}},
        'model': {'calls_logged_by_agents': calls, 'llm_ms_total': round(llm_total, 1),
                  'replay': rstats,
                  'extra_wait_per_call_ms': round((llm_total - rstats['served_delay_ms']) / calls, 2)
                  if rstats and 'served_delay_ms' in rstats and calls else None},
        'memory': {'peak_footprint_mb': peak['footprint_mb'], 'peak_resident_mb': peak['resident_mb'],
                   'per_agent_footprint_mb': round(peak['footprint_mb'] / o.n, 2),
                   'peak_processes': peak['processes'], 'swap_before_mb': base_sys['swap_used_mb'],
                   'peak_swap_mb': peak['swap_used_mb'], 'swap_growth_mb': round(peak['swap_used_mb'] - (base_sys['swap_used_mb'] or 0), 1),
                   'peak_compressed_mb': peak['compressed_mb'], 'peak_pressure': peak['pressure'],
                   'sampler': sampler.source},
        'cpu': {'children_cpu_s': round(ru.ru_utime + ru.ru_stime, 2), 'peak_pct': peak['cpu_pct'],
                'avg_pct': round(100 * (ru.ru_utime + ru.ru_stime) / t_end, 1) if t_end else None},
        'mac': {k: pre[k] for k in ('macos', 'machine', 'chip', 'cores', 'ram_gb', 'power', 'kern.maxproc', 'kern.maxprocperuid',
                                    'kern.maxfiles', 'kern.maxfilesperproc', 'kern.ipc.somaxconn', 'ulimit_nofile', 'ulimit_nproc',
                                    'processes_mine', 'pressure')},
        'batch_stderr_tail': batch_stderr_tail,
    }
    if o.api == 'live':
        logd = out / (sid + '.relay')
        cl = [json.loads(l) for l in (logd / 'calls.jsonl').read_text().splitlines()] if (logd / 'calls.jsonl').exists() else []
        rec['model']['live'] = {'calls': len(cl), 'statuses': sorted({c['status'] for c in cl}),
                                'cost_usd': round(sum((c['response'].get('usage') or {}).get('cost') or 0 for c in cl
                                                      if isinstance(c.get('response'), dict)), 4),
                                'model_ms_median': med([c['ms'] for c in cl])}
        rec['model']['live']['credit_after'] = credit()
    rec_f.write_text(json.dumps(rec, indent=1))
    with open(out / (sid + '.agents.jsonl'), 'w') as f:
        for a in agents:
            f.write(json.dumps({k: (str(v) if isinstance(v, Path) else v) for k, v in a.items() if k not in ('ws', 'runs')}) + '\n')
    if o.keep:
        print('workspaces kept in', base)
    else:
        shutil.rmtree(base, ignore_errors=True)
    t = rec['time']
    print('%s: %s. %d/%d passed hidden tests. Batch %.1f s; outside the model per agent median %s ms, p95 %s ms. '
          'Peak memory %.0f MB (%.1f MB per agent), %d processes, swap +%.0f MB, pressure %s. %s' % (
              sid, outcome, passed, o.n, t['batch_wall_s'], t['outside_model_ms']['median'], t['outside_model_ms']['p95'],
              peak['footprint_mb'], rec['memory']['per_agent_footprint_mb'], peak['processes'], rec['memory']['swap_growth_mb'],
              peak['pressure'], statuses), flush=True)
    sys.exit(0 if rec['ok'] else 3)


if __name__ == '__main__':
    main()
