"""Development ramp for `tinycoder batch` (optimize step only). Standard library only, Python 3.9+.

  python3 tests/ramp_dev.py bin/tinycoder [--levels 10,50,200,500] [--speed 1.0]

At each level it starts one `batch` with that many agents (concurrency = agents) on the two practice tasks, against
the replay model with the scripted recordings, and samples every 0.5 s the memory (footprint, as Activity Monitor
shows it, and resident) and CPU of the program and every process it starts. It prints, per level: batch time, time
outside the model per agent (median, p95), peak memory, memory per agent, peak processes, CPU. Not the official
measurement: that uses other tasks and real recorded replies.
"""
import argparse, os, shutil, statistics, sys, threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'ramp'))
import check_scale as C  # noqa: E402
import measure_lib as M  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bin')
    ap.add_argument('--levels', default='10,50,200,500')
    ap.add_argument('--speed', type=float, default=1.0)
    o = ap.parse_args()
    C.BIN = str(Path(o.bin).resolve())
    print('%7s %8s %14s %10s %11s %11s %9s %8s %7s' % ('agents', 'done', 'batch s', 'outside ms', 'p95 ms', 'peak MB',
                                                        'MB/agent', 'procs', 'CPU %'))
    for n in [int(x) for x in o.levels.split(',')]:
        rp = C.Replay([HERE / 'recordings'], speed=o.speed)
        samples = []
        sam = M.Sampler(os.getpid(), samples.append, every=0.5, exclude=[rp.p.pid])
        holder = {}

        def go():
            holder['r'] = C.batch([('d%04d' % i, HERE / 'tasks' / ('practice-fail' if i % 2 else 'practice-pass')) for i in range(n)],
                                  rp, n, timeout=1800)
        sam.start()
        t = threading.Thread(target=go)
        t.start()
        t.join()
        sam.stop()
        rp.stop()
        r = holder.get('r')
        if not r:
            print('%7d   batch did not run' % n)
            continue
        fin = [d for d in r['lines'].values() if d.get('status') == 'done']
        out = sorted(d['wall_ms'] - d['llm_ms'] for d in fin)
        cpu = [s['cpu_pct'] for s in samples if s.get('cpu_pct') is not None]
        print('%7d %8s %14.1f %10.0f %11.0f %11.0f %9.2f %8d %7.0f' % (
            n, '%d/%d' % (len(fin), n), r['secs'], statistics.median(out) if out else -1, out[int(0.95 * (len(out) - 1))] if out else -1,
            sam.peak['footprint_mb'], sam.peak['footprint_mb'] / n, sam.peak['processes'],
            statistics.mean(cpu) if cpu else -1), flush=True)
        shutil.rmtree(r['base'], ignore_errors=True)


if __name__ == '__main__':
    main()
