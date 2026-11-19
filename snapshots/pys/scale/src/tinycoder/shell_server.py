"""MCP server 2: `shell` (SPEC section 3)."""

import json
import os
import selectors
import shutil
import signal
import time

from .mcpserve import ToolError, build_server, serve
from .policy import argv_problem

TIMEOUT_S = 30
TAIL_CHARS = 8000
ENV_KEYS = ("PATH", "HOME", "LANG")

TOOLS = [
    {
        "name": "run_tests",
        "description": (
            "Run the task's test command in the workspace. Returns exit_code, passed (exit_code == 0), "
            "the combined stdout+stderr (last 8000 characters) and duration_ms. 30 s timeout."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_command",
        "description": (
            "Run one command in the workspace without a shell (argv is passed directly). Allowed: ls, "
            "cat, head, tail, wc, diff, python3, and git status|diff|log|show. Returns exit_code, "
            "stdout and stderr (last 8000 characters each) and duration_ms. 30 s timeout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Program and arguments, e.g. [\"git\", \"diff\"].",
                }
            },
            "required": ["argv"],
        },
    },
]


def _tail(data):
    text = data.decode("utf-8", "replace") if data else ""
    return text[-TAIL_CHARS:]


def _find_executable(name, path):
    """Resolve argv[0] the way exec does: names with a slash are used as given (relative
    to the cwd, which is the workspace), others are looked up on the child's PATH."""
    if "/" in name:
        return name
    return shutil.which(name, path=path)


class _Tail:
    """Collects a stream but keeps only its end: callers use the last 8,000 characters, and
    64 kB of bytes always decodes to more than that, so memory stays bounded."""

    KEEP = 1 << 16

    def __init__(self):
        self.chunks = []
        self.size = 0

    def add(self, chunk):
        self.chunks.append(chunk)
        self.size += len(chunk)
        if self.size > 4 * self.KEEP:
            data = b"".join(self.chunks)[-self.KEEP:]
            self.chunks, self.size = [data], len(data)

    def bytes(self):
        return b"".join(self.chunks)


def run_process(argv, combined):
    """Run argv in the current directory (the workspace).

    Return (exit_code, stdout bytes, stderr bytes, duration_ms); with `combined`, stderr is
    sent into the stdout pipe and the stderr result is empty. posix_spawn is used instead of
    subprocess (fork + exec of a ~50 MB server): ~0.7 ms less per command. The child gets its
    own session, so a timeout SIGKILLs the whole process group.
    """
    env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    start = time.monotonic()
    exe = _find_executable(argv[0], env.get("PATH", os.defpath))
    if exe is None:
        raise ToolError(f"cannot run {argv[0]}: No such file or directory")
    out_r, out_w = os.pipe()  # non-inheritable; dup2 in the child makes the copies inheritable
    err_r, err_w = (None, out_w) if combined else os.pipe()
    actions = [
        (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
        (os.POSIX_SPAWN_DUP2, out_w, 1),
        (os.POSIX_SPAWN_DUP2, err_w, 2),
    ]
    try:
        pid = os.posix_spawn(exe, argv, env, file_actions=actions, setsid=True)
    except OSError as exc:
        os.close(out_r)
        if err_r is not None:
            os.close(err_r)
        raise ToolError(f"cannot run {argv[0]}: {exc.strerror or exc}")
    finally:
        os.close(out_w)
        if not combined:
            os.close(err_w)

    tails = {out_r: _Tail()}
    if err_r is not None:
        tails[err_r] = _Tail()
    timed_out = False
    deadline = start + TIMEOUT_S
    with selectors.DefaultSelector() as sel:
        for fd in tails:
            sel.register(fd, selectors.EVENT_READ)
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if timed_out:
                    break  # a process that left the group still holds a pipe: give up on it
                timed_out = True
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
                deadline = time.monotonic() + 5  # short grace period to drain the pipes
                continue
            for key, _ in sel.select(remaining):
                chunk = os.read(key.fd, 1 << 16)
                if chunk:
                    tails[key.fd].add(chunk)
                else:
                    sel.unregister(key.fd)
    for fd in tails:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    code = -1 if timed_out else os.waitstatus_to_exitcode(status)
    duration_ms = int(round((time.monotonic() - start) * 1000))
    out = tails[out_r].bytes()
    err = tails[err_r].bytes() if err_r is not None else b""
    return code, out, err, duration_ms


class Shell:
    def __init__(self, workspace, task_path):
        self.root = os.path.realpath(workspace)
        if not os.path.isdir(self.root):
            raise SystemExit(f"tinycoder: workspace is not a directory: {workspace}")
        self.task_path = os.path.abspath(task_path)
        # Every command runs in the workspace; posix_spawn has no cwd option, so the server
        # itself works there and children inherit it.
        os.chdir(self.root)
        self._test_command = None

    def test_command(self):
        if self._test_command is None:
            try:
                with open(self.task_path, encoding="utf-8") as f:
                    cmd = json.load(f).get("test_command")
            except (OSError, ValueError) as exc:
                raise ToolError(f"cannot read task file {self.task_path}: {exc}")
            if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
                raise ToolError("task file has no valid test_command")
            self._test_command = cmd
        return self._test_command

    async def run_tests(self, args):
        import anyio.to_thread

        cmd = self.test_command()
        code, out, _, ms = await anyio.to_thread.run_sync(run_process, cmd, True)
        return {"exit_code": code, "passed": code == 0, "output": _tail(out), "duration_ms": ms}

    async def run_command(self, args):
        import anyio.to_thread

        argv = args.get("argv")
        why = argv_problem(argv)
        if why:
            raise ToolError(why)
        code, out, err, ms = await anyio.to_thread.run_sync(run_process, argv, False)
        return {"exit_code": code, "stdout": _tail(out), "stderr": _tail(err), "duration_ms": ms}


def main(workspace, task_path):
    shell = Shell(workspace, task_path)
    serve("shell", TOOLS, {"run_tests": shell.run_tests, "run_command": shell.run_command})


# --- in-process variant (used by `batch`) ----------------------------------------------
#
# Many agents share one process, so the server cannot chdir into its workspace and must not
# block the event loop. Commands are started with asyncio's subprocess support (cwd set per
# call, own session so a timeout kills the whole group) and their pipes are read by the
# event loop. A limiter shared by all agents bounds how many commands run at once.

SPAWN_RETRY_S = 10.0  # how long to retry a spawn refused with EAGAIN (process limit)


def _spawn_in(cwd, exe, argv, env, actions):
    """posix_spawn with the child's working directory set to `cwd`.

    os.posix_spawn has no cwd option, and subprocess's fork+exec of a large batch process
    costs ~5 ms of blocked event loop per command (posix_spawn: ~0.1 ms). So the process
    changes directory just around the spawn call and changes back at once. This runs on the
    event-loop thread with no await inside, so no other agent's code can run in between,
    and the batch keeps no other thread that depends on the working directory.
    """
    here = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.chdir(cwd)
        try:
            return os.posix_spawn(exe, argv, env, file_actions=actions, setsid=True)
        finally:
            os.fchdir(here)
    finally:
        os.close(here)


class _Reader:
    """Reads one pipe from the event loop into a _Tail until EOF."""

    def __init__(self, loop, fd):
        self.loop, self.fd, self.tail = loop, fd, _Tail()
        self.done = loop.create_future()
        os.set_blocking(fd, False)
        loop.add_reader(fd, self._ready)

    def _ready(self):
        try:
            chunk = os.read(self.fd, 1 << 16)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if chunk:
            self.tail.add(chunk)
        else:
            self.close()

    def close(self):
        if self.fd is not None:
            self.loop.remove_reader(self.fd)
            os.close(self.fd)
            self.fd = None
        if not self.done.done():
            self.done.set_result(None)


async def _reap(pid):
    """Wait for `pid` to exit without a thread: the pipes are already at EOF, so the child
    is exiting; poll waitpid with a short back-off."""
    import anyio

    delay = 0.001
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        await anyio.sleep(delay)
        delay = min(delay * 2, 0.05)


async def run_process_async(argv, combined, cwd, limiter):
    """Async twin of run_process: same results, explicit cwd, pipes read by the event loop."""
    import asyncio
    import errno

    import anyio

    env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    exe = _find_executable(argv[0], env.get("PATH", os.defpath))
    if exe is None:
        raise ToolError(f"cannot run {argv[0]}: No such file or directory")
    loop = asyncio.get_running_loop()
    async with limiter:
        waited, delay = 0.0, 0.05
        while True:
            out_r, out_w = os.pipe()
            err_r, err_w = (None, out_w) if combined else os.pipe()
            actions = [
                (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_DUP2, out_w, 1),
                (os.POSIX_SPAWN_DUP2, err_w, 2),
            ]
            start = time.monotonic()
            try:
                pid = _spawn_in(cwd, exe, argv, env, actions)
                break
            except OSError as exc:
                os.close(out_r)
                if err_r is not None:
                    os.close(err_r)
                if exc.errno == errno.EAGAIN and waited < SPAWN_RETRY_S:
                    await anyio.sleep(delay)  # out of processes for now: wait for others to exit
                    waited += delay
                    delay = min(delay * 2, 1.0)
                    continue
                raise ToolError(f"cannot run {argv[0]}: {exc.strerror or exc}")
            finally:
                os.close(out_w)
                if not combined:
                    os.close(err_w)

        readers = [_Reader(loop, out_r)]
        if err_r is not None:
            readers.append(_Reader(loop, err_r))
        code = None
        timed_out = False
        try:
            with anyio.move_on_after(TIMEOUT_S - (time.monotonic() - start)) as scope:
                for r in readers:
                    await asyncio.shield(r.done)  # a timeout must not cancel the future itself
                code = await _reap(pid)
            if scope.cancelled_caught:
                timed_out = True
                _killpg(pid)
                with anyio.move_on_after(5):  # short grace period to drain the pipes
                    for r in readers:
                        await asyncio.shield(r.done)
        finally:
            for r in readers:
                r.close()  # a process that left the group may still hold a pipe
            if code is None:  # timed out, or the caller was cancelled
                _killpg(pid)
                with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                    code = await _reap(pid)
        duration_ms = int(round((time.monotonic() - start) * 1000))
        out = readers[0].tail.bytes()
        err = readers[1].tail.bytes() if not combined else b""
        return (-1 if timed_out else code), out, err, duration_ms


def _killpg(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


class AsyncShell(Shell):
    """The shell tools for one agent inside a shared process (no chdir)."""

    def __init__(self, workspace, task_path, limiter):
        self.root = os.path.realpath(workspace)
        if not os.path.isdir(self.root):
            raise RuntimeError(f"workspace is not a directory: {workspace}")
        self.task_path = os.path.abspath(task_path)
        self.limiter = limiter
        self._test_command = None

    async def run_tests(self, args):
        cmd = self.test_command()
        code, out, _, ms = await run_process_async(cmd, True, self.root, self.limiter)
        return {"exit_code": code, "passed": code == 0, "output": _tail(out), "duration_ms": ms}

    async def run_command(self, args):
        argv = args.get("argv")
        why = argv_problem(argv)
        if why:
            raise ToolError(why)
        code, out, err, ms = await run_process_async(argv, False, self.root, self.limiter)
        return {"exit_code": code, "stdout": _tail(out), "stderr": _tail(err), "duration_ms": ms}


def build(workspace, task_path, limiter):
    """The `shell` server for one agent, not yet running (used in-process by batch)."""
    shell = AsyncShell(workspace, task_path, limiter)
    return build_server("shell", TOOLS, {"run_tests": shell.run_tests, "run_command": shell.run_command})
