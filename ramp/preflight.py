"""Checks the Mac before a sitting and prints what it found. Changes nothing. Run from the repo root.

  /usr/bin/python3 ramp/preflight.py [--json out.json] [--strict]

Refuses (exit 1) with --strict when: not on AC power, Low Power Mode on, swap in use above 1 GB (reboot first),
memory pressure not normal, a build is missing, or the frozen files changed. Everything it reads goes into each
step's record, so the film can show the exact limits the Mac had.
"""
import argparse, json, os, platform, resource, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ramp'))
import measure_lib as M  # noqa: E402
from builds import BUILDS  # noqa: E402

SWAP_MAX_MB = 1024


def sh(*a):
    return subprocess.run(list(a), capture_output=True, text=True).stdout.strip()


def facts():
    me = os.getuid()
    procs = sh('/bin/ps', '-axo', 'uid=').split()
    top = sh('/bin/ps', '-axro', 'pcpu=,rss=,comm=').splitlines()[:8]
    lim_n = resource.getrlimit(resource.RLIMIT_NOFILE)
    lim_u = resource.getrlimit(resource.RLIMIT_NPROC)
    return {
        'macos': platform.mac_ver()[0], 'machine': sh('/usr/sbin/sysctl', '-n', 'hw.model'),
        'chip': sh('/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'),
        'cores': int(sh('/usr/sbin/sysctl', '-n', 'hw.ncpu') or 0),
        'ram_gb': round(int(sh('/usr/sbin/sysctl', '-n', 'hw.memsize') or 0) / 2 ** 30, 1),
        'power': 'AC' if 'AC Power' in sh('/usr/bin/pmset', '-g', 'batt') else 'battery',
        'low_power_mode': ' lowpowermode         1' in sh('/usr/bin/pmset', '-g'),
        'kern.maxproc': int(M.sysctl('kern.maxproc') or 0), 'kern.maxprocperuid': int(M.sysctl('kern.maxprocperuid') or 0),
        'kern.maxfiles': int(M.sysctl('kern.maxfiles') or 0), 'kern.maxfilesperproc': int(M.sysctl('kern.maxfilesperproc') or 0),
        'kern.ipc.somaxconn': int(M.sysctl('kern.ipc.somaxconn') or 0),
        'kern.num_taskthreads': int(M.sysctl('kern.num_taskthreads') or 0),
        'ulimit_nofile': list(lim_n), 'ulimit_nproc': list(lim_u),
        'processes_all': len(procs), 'processes_mine': sum(1 for u in procs if u == str(me)),
        'swap_used_mb': M.swap_used_mb(), 'pressure': M.pressure_level(), 'vm': M.vm_stat(),
        'uptime': sh('/usr/bin/uptime'), 'top_processes': top,
        'sampler_source': 'libproc' if M.proc_usage(os.getpid()) else 'ps',
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json')
    ap.add_argument('--strict', action='store_true')
    o = ap.parse_args()
    f = facts()
    stop = []
    if f['power'] != 'AC':
        stop.append('not on AC power')
    if f['low_power_mode']:
        stop.append('Low Power Mode is on')
    if (f['swap_used_mb'] or 0) > SWAP_MAX_MB:
        stop.append('swap in use %.0f MB (over %d): restart the Mac first' % (f['swap_used_mb'], SWAP_MAX_MB))
    if f['pressure'] not in (1, None):
        stop.append('memory pressure level %s (not normal)' % f['pressure'])
    missing = ['%s round %s' % k for k, v in BUILDS.items() if v['required'] and not (v['dir'] / 'bin' / 'tinycoder').exists()]
    if missing:
        stop.append('missing builds: ' + ', '.join(missing))
    fr = subprocess.run([sys.executable, str(ROOT / 'tools/freeze.py'), '--check'], capture_output=True, text=True)
    f['frozen'] = fr.returncode == 0
    if fr.returncode != 0:
        stop.append('frozen files changed or not frozen: ' + (fr.stdout + fr.stderr).strip()[-300:])
    f['refusals'] = stop
    for k, v in f.items():
        if k not in ('vm', 'top_processes'):
            print('%-22s %s' % (k, v))
    print('top processes by CPU:')
    for line in f['top_processes']:
        print('   ', line)
    agents_fit = (f['kern.maxprocperuid'] - f['processes_mine']) // 3
    print('round 1 needs 3 processes per agent (+1 while it runs tests): about %d agents fit under kern.maxprocperuid now' % agents_fit)
    if f['kern.ipc.somaxconn'] < 1024:
        print('NOTE kern.ipc.somaxconn is %d: the replay model can queue only that many new connections at once (RUN.md step 2)' % f['kern.ipc.somaxconn'])
    print('READY' if not stop else 'NOT READY: ' + '; '.join(stop))
    if o.json:
        Path(o.json).write_text(json.dumps(f, indent=1))
    sys.exit(1 if (stop and o.strict) else 0)


if __name__ == '__main__':
    main()
