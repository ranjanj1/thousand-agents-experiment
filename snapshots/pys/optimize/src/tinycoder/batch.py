"""`tinycoder batch`: run many agents in one process (SPEC-SCALE.md).

One asyncio event loop runs every agent. Each agent is the same `Agent` as `tinycoder run`
(same requests, retries, hooks, step limit, runs file, final object), with three differences:

- its run-id comes from the manifest;
- its `files` and `shell` servers are SDK `Server` objects in this process, built for its own
  workspace and task, and reached through the SDK's in-process client transport
  (`mcp.Client(server)`). No child process per server: 1,000 agents would need 2,000 Python
  processes, over the per-user process limit;
- all agents share one `AsyncAnthropic` client and its connection pool.
"""

import json
import os
import sys
import time

MAX_AGENTS = 2000
MAX_CONCURRENCY = 1000
AGENT_KEYS = ("run_id", "task", "workspace", "runs")
MAX_LIVE_COMMANDS = 128
YOUNG_S = 0.15


class ManifestError(Exception):
    pass


def load_manifest(path):
    """Return the agent list, or raise ManifestError saying why the manifest is unusable."""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        raise ManifestError('manifest must be an object with an "agents" array')
    agents = data["agents"]
    if not 1 <= len(agents) <= MAX_AGENTS:
        raise ManifestError(f"manifest must list between 1 and {MAX_AGENTS} agents, not {len(agents)}")
    seen = set()
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            raise ManifestError(f"agents[{i}] must be an object")
        for key in AGENT_KEYS:
            value = agent.get(key)
            if not isinstance(value, str) or value == "":
                raise ManifestError(f"agents[{i}].{key} must be a non-empty string")
        run_id = agent["run_id"]
        if "/" in run_id or "\\" in run_id or "\x00" in run_id or run_id in (".", ".."):
            raise ManifestError(f"agents[{i}].run_id is not usable as a file name: {run_id!r}")
        key = (os.path.abspath(agent["runs"]), run_id)
        if key in seen:  # both would append to the same runs file
            raise ManifestError(f"agents[{i}] repeats run_id {run_id!r} in the same runs folder")
        seen.add(key)
    return [{key: agent[key] for key in AGENT_KEYS} for agent in agents]


def parse_concurrency(text):
    try:
        n = int(text)
    except (TypeError, ValueError):
        raise ManifestError(f"--concurrency must be an integer, not {text!r}")
    if not 1 <= n <= MAX_CONCURRENCY:
        raise ManifestError(f"--concurrency must be between 1 and {MAX_CONCURRENCY}, not {n}")
    return n


def _raise_fd_limit():
    """Each in-flight model request holds a socket and each command up to three pipes: lift
    the soft open-files limit towards the hard limit (never above it)."""
    try:
        import resource
    except ImportError:
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    for want in (hard, 10240):  # macOS refuses values above kern.maxfilesperproc
        if want == resource.RLIM_INFINITY:
            continue
        if soft != resource.RLIM_INFINITY and soft >= want:
            return
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            return
        except (ValueError, OSError):
            continue


def command_slots(concurrency):
    """(young, total, young_s) for shell_server.CommandSlots.

    young = 2 x CPU count: test runs are mostly CPU-bound, so more at once only share the
    cores (measured at 500 agents: 8, 16, 32 and 64 slots all gave ~1.0-1.2 s median
    run_tests) while each extra live test process costs memory. young_s: a command stops
    counting as young after 0.25 s, so slow or sleeping commands do not hold the queue.
    total = 128 live command processes at most, far below the per-user process limit (1,333
    here) that the harness, the API and the tests' own children share.
    TINYCODER_COMMAND_SLOTS="young[,total[,young_ms]]" overrides them (for experiments).
    """
    young, total, young_s = os.cpu_count() or 4, MAX_LIVE_COMMANDS, YOUNG_S
    text = os.environ.get("TINYCODER_COMMAND_SLOTS", "")
    try:
        parts = [int(x) for x in text.split(",")] if text else []
    except ValueError:
        parts = []
    if parts:
        young = parts[0]
        total = parts[1] if len(parts) > 1 else max(young, total)
        young_s = parts[2] / 1000 if len(parts) > 2 else young_s
    young = max(1, min(concurrency, young))
    return young, max(young, min(concurrency, total)), young_s


async def _batch(agents, concurrency):
    import anyio
    import httpx2

    from anthropic._base_client import DefaultAsyncHttpxClient

    from . import files_server, shell_server
    from .agent import Agent, error_final, make_client

    slots = shell_server.CommandSlots(*command_slots(concurrency))

    def servers(workspace, task_json):
        return files_server.build(workspace), shell_server.build(workspace, task_json, slots)

    # Model clients: one per worker slot, each with a one-connection pool. A worker runs one
    # agent at a time and an agent has at most one request in flight, so a request never
    # waits for a connection, and the slot's next agent reuses the open connection.
    # httpcore's pool scans (and poll()s) every pooled connection each time it hands one
    # out, so a single shared pool of 1,000 connections made every request O(1,000); a
    # client costs ~6 kB. The TLS context (the SDK default) is built once and shared.
    import httpcore2

    ssl_context = httpcore2.default_ssl_context()
    clients = [
        make_client(http_client=DefaultAsyncHttpxClient(
            limits=httpx2.Limits(max_connections=1, max_keepalive_connections=1),
            verify=ssl_context,
        ))
        for _ in range(min(concurrency, len(agents)))
    ]
    out = sys.stdout
    next_index = 0

    def emit(final, run_id):
        out.write(json.dumps({**final, "run_id": run_id}, ensure_ascii=False) + "\n")
        out.flush()

    async def run_one(spec, client):
        t0 = time.perf_counter()
        try:
            agent = Agent(
                spec["task"], spec["workspace"], spec["runs"], t0,
                run_id=spec["run_id"], client=client, servers=servers,
            )
        except Exception as exc:  # e.g. the runs directory cannot be created
            final = error_final(f"{type(exc).__name__}: {exc}", t0)
        else:
            try:
                final = await agent.execute()
            except Exception as exc:  # execute() catches everything; this is a last guard
                final = error_final(f"{type(exc).__name__}: {exc}", t0)
        emit(final, spec["run_id"])

    async def worker(slot):
        # Each worker takes the next agent in manifest order, so at most `concurrency`
        # agents are in progress and they start in order.
        nonlocal next_index
        client = clients[slot]
        while next_index < len(agents):
            spec = agents[next_index]
            next_index += 1
            await run_one(spec, client)

    try:
        async with anyio.create_task_group() as tg:
            for slot in range(min(concurrency, len(agents))):
                tg.start_soon(worker, slot)
    finally:
        with anyio.CancelScope(shield=True):
            for client in clients:
                await client.close()


def main(manifest_path, concurrency_text):
    try:
        concurrency = parse_concurrency(concurrency_text)
        agents = load_manifest(manifest_path)
    except ManifestError as exc:
        print(f"tinycoder batch: {exc}", file=sys.stderr)
        return 1

    import gc
    import logging

    from . import fastimport

    # Skip the SDKs' re-export package __init__ files, as `run` and `serve` do (see
    # fastimport.py): ~20 MB and ~0.45 s less start-up, the same SDK code otherwise.
    fastimport.shim(
        "mcp", "mcp.client", "mcp.server", "anthropic", "anthropic.lib", "anthropic.resources",
        *fastimport.subpackages("anthropic", "types"),
    )

    import anyio
    import anthropic._client  # noqa: F401  (loaded before gc.freeze below)
    import anthropic.resources.messages  # noqa: F401
    import mcp.client.client  # noqa: F401

    from . import agent, files_server, shell_server  # noqa: F401

    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    _raise_fd_limit()
    # Long-lived SDK objects (pydantic models, modules) would be rescanned by every full
    # collection; freeze what start-up allocated and collect the young generation less often.
    gc.collect()
    gc.freeze()
    gc.enable()
    gc.set_threshold(10_000, 20, 50)
    anyio.run(_batch, agents, concurrency)
    return 0
