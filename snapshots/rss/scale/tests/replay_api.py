"""The replay model: a fake Claude Messages API that answers every agent with a reply Sonnet 5.5 really gave in
Experiment 4, after the delay that reply really took. Standard library only, Python 3.9+, one asyncio event loop.

  /usr/bin/python3 replay/replay_api.py --recordings recordings/ --log <file.jsonl> [--port 18490] [--speed 1.0] [--seed 5]

Prints `PORT <n>` once listening. GET /stats returns live counters as JSON (the harness reads them).

How an agent is matched to a recording, with no change to any build:
  - A request with no assistant message is a new agent. Its task is found from the first user message (the exact
    TASK.md text) and it gets the next recording for that task, cycling through them in a fixed shuffled order.
  - Every tool_use id in a reply is rewritten to `toolu_r5_<slot>_<step>_<i>`. SPEC 4.3 makes the agent send those ids
    back, so the next request names its own slot and step. Nothing else in a reply is changed.
  - A retry (same messages again) gets the same step again (from step 1 on: a retried first call looks like a new
    agent, so `fail_first` belongs on step 1 or later). A step may list `fail_first: [529, ...]`: those statuses
    are returned on the first attempts, then the real reply (used by the checks, never by recordings of real runs).
    A step may instead be `{"status": 400}`: a permanent API error.

--speed scales every delay (1.0 = as recorded, 0 = no delay). Every agent of every language gets the same replies at
the same speed; only the language's own overhead differs.

Each request is logged as one JSON line: n, slot, recording, step, t_in, t_out, delay_ms, in_flight, problems.
`problems` uses the request rules of tests/fake_api.py (SPEC 4.1) plus replay checks (wrong step, tool_result count).
"""
import argparse, asyncio, json, random, resource, signal, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for cand in (HERE.parent / 'tests', HERE):
    if (cand / 'fake_api.py').exists():
        sys.path.insert(0, str(cand))
        break
from fake_api import check as request_problems  # noqa: E402  (the frozen SPEC 4.1 request rules)

REASON = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 429: 'Too Many Requests', 500: 'Internal Server Error',
          502: 'Bad Gateway', 503: 'Service Unavailable', 529: 'Overloaded'}
ERR_TYPE = {400: 'invalid_request_error', 404: 'not_found_error', 429: 'rate_limit_error', 529: 'overloaded_error'}


def text_of(content):
    if isinstance(content, str):
        return content
    return ''.join(b.get('text', '') for b in content or [] if isinstance(b, dict) and b.get('type') == 'text')


def load(dirs, seed):
    """{task_text: [recording, ...]} in a fixed shuffled order per task."""
    book = {}
    for d in dirs:
        for f in sorted(Path(d).rglob('*.json')):
            if f.name == 'INDEX.json':
                continue
            r = json.loads(f.read_text())
            r['name'] = r.get('source') or f.stem
            book.setdefault(r['task_text'], []).append(r)
    rnd = random.Random(seed)
    for k in book:
        book[k].sort(key=lambda r: r['name'])
        rnd.shuffle(book[k])
    return book


class Replay:
    def __init__(self, book, speed, log):
        self.book, self.speed, self.log = book, speed, log
        self.next_rec = {k: 0 for k in book}
        self.slots = []                      # slot -> recording
        self.attempts = {}                   # (slot, step) -> attempts so far
        self.n = 0
        self.in_flight = self.max_in_flight = 0
        self.problems = 0
        self.served_delay_ms = 0.0
        self.by_status = {}
        self.t_first = self.t_last = None

    def stats(self):
        ru = resource.getrusage(resource.RUSAGE_SELF)
        return {'requests': self.n, 'agents': len(self.slots), 'in_flight': self.in_flight, 'max_in_flight': self.max_in_flight,
                'problems': self.problems, 'served_delay_ms': round(self.served_delay_ms, 1), 'by_status': self.by_status,
                'cpu_s': round(ru.ru_utime + ru.ru_stime, 3), 'max_rss_mb': round(ru.ru_maxrss / 1048576, 1),
                't_first': self.t_first, 't_last': self.t_last}

    def route(self, body, headers, path):
        """Returns (status, reply object, delay_ms, log record)."""
        probs = [] if path.split('?')[0] == '/v1/messages' else ['path is ' + path]
        probs += request_problems(headers, body)
        msgs = body.get('messages') or []
        asst = [m for m in msgs if m.get('role') == 'assistant']
        rec = {'slot': None, 'recording': None, 'step': None}
        if not asst:
            task = text_of(msgs[0].get('content')) if msgs else ''
            recs = self.book.get(task)
            if not recs:
                probs.append('unknown task (first user message matches no recording)')
                return 400, err(400, 'replay: unknown task'), 0, dict(rec, problems=probs)
            r = recs[self.next_rec[task] % len(recs)]
            self.next_rec[task] += 1
            slot, step = len(self.slots), 0
            self.slots.append(r)
        else:
            ids = [b.get('id', '') for b in asst[-1].get('content') or [] if isinstance(b, dict) and b.get('type') == 'tool_use']
            try:
                _, _, s, k, _ = ids[0].split('_')
                slot, prev = int(s), int(k)
            except (IndexError, ValueError):
                probs.append('last assistant message has no replay tool_use id')
                return 400, err(400, 'replay: cannot find the agent'), 0, dict(rec, problems=probs)
            step = len(asst)
            if prev != step - 1:
                probs.append('tool_use ids from step %d but %d assistant messages' % (prev, len(asst)))
            last_user = msgs[-1] if msgs and msgs[-1].get('role') == 'user' else {}
            results = [b for b in last_user.get('content') or [] if isinstance(b, dict) and b.get('type') == 'tool_result']
            if len(results) != len(ids) or [b.get('tool_use_id') for b in results] != ids:
                probs.append('tool_results do not answer the last tool_use blocks one for one, in order')
            if slot >= len(self.slots):
                probs.append('unknown slot %d' % slot)
                return 400, err(400, 'replay: unknown agent'), 0, dict(rec, problems=probs)
            r = self.slots[slot]
        rec.update(slot=slot, recording=r['name'], step=step)
        if step >= len(r['steps']):
            probs.append('recording exhausted')
            return 500, err(500, 'replay: recording exhausted'), 0, dict(rec, problems=probs)
        s = r['steps'][step]
        a = self.attempts.get((slot, step), 0)
        self.attempts[(slot, step)] = a + 1
        rec['attempt'] = a + 1
        delay = (s.get('delay_ms') or 0) * self.speed
        fails = s.get('fail_first') or []
        if a < len(fails):
            return fails[a], err(fails[a], 'replay: scripted error'), delay, dict(rec, problems=probs)
        if 'status' in s:
            return s['status'], err(s['status'], 'replay: scripted error'), delay, dict(rec, problems=probs)
        content = json.loads(json.dumps(s['content']))
        i = 0
        for b in content:
            if b.get('type') == 'tool_use':
                b['id'] = 'toolu_r5_%d_%d_%d' % (slot, step, i)
                i += 1
        usage = dict({'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0},
                     **(s.get('usage') or {}))
        msg = {'id': 'msg_r5_%d_%d' % (slot, step), 'type': 'message', 'role': 'assistant', 'model': 'claude-sonnet-5-5',
               'content': content, 'stop_reason': s['stop_reason'], 'stop_sequence': None, 'usage': usage}
        return 200, msg, delay, dict(rec, problems=probs)


def err(status, text):
    return {'type': 'error', 'error': {'type': ERR_TYPE.get(status, 'api_error'), 'message': text}}


async def read_body(reader, headers):
    if headers.get('transfer-encoding', '').lower() == 'chunked':
        out = b''
        while True:
            size = int((await reader.readline()).split(b';')[0].strip() or b'0', 16)
            if size == 0:
                while (await reader.readline()) not in (b'\r\n', b'\n', b''):
                    pass
                return out
            out += await reader.readexactly(size)
            await reader.readline()
    return await reader.readexactly(int(headers.get('content-length') or 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--recordings', action='append', required=True, help='folder of recordings (repeatable)')
    ap.add_argument('--log', required=True)
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--seed', type=int, default=5)
    o = ap.parse_args()
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 65536) if hard != resource.RLIM_INFINITY else 65536, hard))
    except (ValueError, OSError):
        pass
    book = load(o.recordings, o.seed)
    if not book:
        sys.exit('no recordings in %s' % o.recordings)
    log = open(o.log, 'a', buffering=1 << 16)
    rp = Replay(book, o.speed, log)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))   # the harness stops it with SIGTERM; the log is flushed below

    async def conn(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                method, path = line.decode('latin-1').split(' ')[:2]
                headers = {}
                while True:
                    h = await reader.readline()
                    if h in (b'\r\n', b'\n', b''):
                        break
                    k, _, v = h.decode('latin-1').partition(':')
                    headers[k.strip().lower()] = v.strip()
                if headers.get('expect', '').lower() == '100-continue':
                    writer.write(b'HTTP/1.1 100 Continue\r\n\r\n')
                raw = await read_body(reader, headers)
                if method == 'GET' and path.startswith('/stats'):
                    status, obj = 200, rp.stats()
                elif method != 'POST':
                    status, obj = 404, err(404, 'replay: POST /v1/messages only')
                else:
                    t_in = time.time()
                    rp.t_first = rp.t_first or t_in
                    n = rp.n
                    rp.n += 1
                    rp.in_flight += 1
                    rp.max_in_flight = max(rp.max_in_flight, rp.in_flight)
                    try:
                        body = json.loads(raw or b'{}')
                    except ValueError:
                        body = {}
                    status, obj, delay, rec = rp.route(body, headers, path)
                    if delay:
                        await asyncio.sleep(delay / 1000.0)
                    rp.in_flight -= 1
                    rp.served_delay_ms += delay
                    rp.by_status[str(status)] = rp.by_status.get(str(status), 0) + 1
                    rp.problems += len(rec['problems'])
                    rp.t_last = time.time()
                    log.write(json.dumps(dict(rec, n=n, status=status, t_in=round(t_in, 4), t_out=round(rp.t_last, 4),
                                              delay_ms=round(delay, 1), in_flight=rp.in_flight + 1)) + '\n')
                data = json.dumps(obj).encode()
                extra = b'retry-after: 0\r\nx-should-retry: true\r\n' if status in (429, 500, 502, 503, 529) else b''
                writer.write(b'HTTP/1.1 %d %s\r\ncontent-type: application/json\r\ncontent-length: %d\r\nrequest-id: req_replay\r\n%s\r\n'
                             % (status, REASON.get(status, 'Error').encode(), len(data), extra) + data)
                await writer.drain()
                if headers.get('connection', '').lower() == 'close':
                    break
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def serve():
        srv = await asyncio.start_server(conn, '127.0.0.1', o.port, backlog=4096)
        print('PORT %d' % srv.sockets[0].getsockname()[1], flush=True)
        async with srv:
            await srv.serve_forever()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    finally:
        log.flush()


if __name__ == '__main__':
    main()
