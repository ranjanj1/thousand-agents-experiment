"""Memory, CPU and system sampling on macOS. Standard library only, Python 3.9+. Shared by ramp.py and (copied into the
round-2 bundle) tests/ramp_dev.py.

Per process it reads what Activity Monitor's "Memory" column shows (phys_footprint) and resident size, plus CPU time,
with proc_pid_rusage from libproc (no root needed for your own processes). If that call is not available it falls back
to `ps` resident size and marks the samples `"source": "ps"`.

System: vm_stat pages, swap used (sysctl vm.swapusage), the kernel's memory-pressure level (1 normal, 2 warning,
4 critical), the number of processes of this user.
"""
import ctypes, ctypes.util, os, re, subprocess, threading, time

PAGE = os.sysconf('SC_PAGE_SIZE')


class _RU(ctypes.Structure):          # struct rusage_info_v2 (sys/resource.h)
    _fields_ = [('uuid', ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        'user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins', 'wired_size', 'resident_size',
        'phys_footprint', 'proc_start_abstime', 'proc_exit_abstime', 'child_user_time', 'child_system_time',
        'child_pkg_idle_wkups', 'child_interrupt_wkups', 'child_pageins', 'child_elapsed_abstime',
        'diskio_bytesread', 'diskio_byteswritten')]


class _TB(ctypes.Structure):
    _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]


try:
    _libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
    _libc = ctypes.CDLL(ctypes.util.find_library('c'))
    _tb = _TB()
    _libc.mach_timebase_info(ctypes.byref(_tb))
    _NS = _tb.numer / _tb.denom       # mach time units -> ns (125/3 on Apple Silicon)
except OSError:
    _libproc = None


def proc_usage(pid):
    """(footprint_bytes, resident_bytes, cpu_seconds) or None if the process is gone."""
    if _libproc is None:
        return None
    ru = _RU()
    if _libproc.proc_pid_rusage(int(pid), 2, ctypes.byref(ru)) != 0:
        return None
    return ru.phys_footprint, ru.resident_size, (ru.user_time + ru.system_time) * _NS / 1e9


def process_table():
    """{pid: (ppid, rss_kb, command)} for every process."""
    out = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,rss=,comm='], capture_output=True, text=True).stdout
    t = {}
    for line in out.splitlines():
        a = line.split(None, 3)
        if len(a) >= 3:
            t[int(a[0])] = (int(a[1]), int(a[2]), a[3] if len(a) > 3 else '')
    return t


def child_pids(pid):
    """Direct children through libproc (works where `ps` is not allowed), or None."""
    if _libproc is None:
        return None
    buf = (ctypes.c_int * 8192)()
    n = _libproc.proc_listchildpids(int(pid), buf, ctypes.sizeof(buf))
    return None if n < 0 else [buf[i] for i in range(n) if buf[i] > 0]


def tree(root):
    """Every descendant pid of root via libproc; falls back to ps."""
    out, stack = [], [root]
    while stack:
        kids = child_pids(stack.pop())
        if kids is None:
            return descendants(process_table(), root)
        out.extend(kids)
        stack.extend(kids)
    return out


def descendants(table, root, exclude=()):
    kids = {}
    for pid, (ppid, _, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    seen, stack = [], [root]
    skip = set(exclude)
    while stack:
        p = stack.pop()
        for c in kids.get(p, []):
            if c not in skip:
                seen.append(c)
                stack.append(c)
    return seen


def sysctl(name):
    return subprocess.run(['/usr/sbin/sysctl', '-n', name], capture_output=True, text=True).stdout.strip()


def swap_used_mb():
    m = re.search(r'used = ([\d.]+)M', sysctl('vm.swapusage'))
    return float(m.group(1)) if m else None


def pressure_level():
    try:
        return int(sysctl('kern.memorystatus_vm_pressure_level'))
    except ValueError:
        return None


def vm_stat():
    out = subprocess.run(['/usr/bin/vm_stat'], capture_output=True, text=True).stdout
    page = int(re.search(r'page size of (\d+)', out).group(1)) if 'page size of' in out else PAGE
    v = {}
    for line in out.splitlines()[1:]:
        k, _, n = line.partition(':')
        n = n.strip().rstrip('.')
        if n.isdigit():
            v[k.strip().strip('"')] = int(n)
    mb = lambda k: round(v.get(k, 0) * page / 1048576, 1)
    return {'free_mb': mb('Pages free'), 'active_mb': mb('Pages active'), 'inactive_mb': mb('Pages inactive'),
            'wired_mb': mb('Pages wired down'), 'compressed_mb': mb('Pages occupied by compressor'),
            'swapins': v.get('Swapins', 0), 'swapouts': v.get('Swapouts', 0)}


def system_snapshot():
    s = vm_stat()
    s.update(swap_used_mb=swap_used_mb(), pressure=pressure_level())
    return s


class Sampler(threading.Thread):
    """Samples the process tree under `root` (minus `exclude` and their children) every `every` seconds.
    Writes one JSON-ready dict per sample to `sink(dict)` and keeps peaks in .peak."""

    def __init__(self, root, sink, every=0.5, exclude=()):
        super().__init__(daemon=True)
        self.root, self.sink, self.every, self.exclude = root, sink, every, list(exclude)
        self.stop_evt = threading.Event()
        self.t0 = time.time()
        self.peak = {'footprint_mb': 0.0, 'resident_mb': 0.0, 'processes': 0, 'swap_used_mb': 0.0, 'pressure': 0,
                     'compressed_mb': 0.0, 'cpu_pct': 0.0}
        self.last = None
        self._cpu = {}
        self._prev_t = None
        self.source = 'libproc' if _libproc is not None else 'ps'

    def one(self):
        now = time.time()
        excl = set(self.exclude)
        for e in list(excl):
            excl.update(tree(e))
        pids = [p for p in tree(self.root) if p not in excl]
        table = {} if _libproc is not None else process_table()
        fp = rs = 0
        cpu_delta = 0.0
        alive = {}
        for p in pids:
            u = proc_usage(p)
            if u is None:
                rs += table.get(p, (0, 0, ''))[1] * 1024
                continue
            fp += u[0]
            rs += u[1]
            alive[p] = u[2]
            cpu_delta += u[2] - self._cpu.get(p, 0.0)
        dt = (now - self._prev_t) if self._prev_t else None
        self._cpu, self._prev_t = alive, now
        s = {'t': round(now - self.t0, 3), 'processes': len(pids), 'footprint_mb': round(fp / 1048576, 1),
             'resident_mb': round(rs / 1048576, 1), 'cpu_pct': round(100 * cpu_delta / dt, 1) if dt else None,
             'source': self.source}
        s.update(system_snapshot())
        self.last = s
        for k in ('footprint_mb', 'resident_mb', 'processes', 'swap_used_mb', 'pressure', 'compressed_mb', 'cpu_pct'):
            if s.get(k) is not None:
                self.peak[k] = max(self.peak[k], s[k])
        self.sink(s)
        return s

    def run(self):
        while not self.stop_evt.is_set():
            t = time.time()
            try:
                self.one()
            except Exception as e:  # a sample must never kill the run
                self.sink({'t': round(t - self.t0, 3), 'sample_error': repr(e)[:200]})
            self.stop_evt.wait(max(0.0, self.every - (time.time() - t)))

    def stop(self):
        self.stop_evt.set()
        self.join(timeout=5)
