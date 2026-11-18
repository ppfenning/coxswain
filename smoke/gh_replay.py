"""GitHub's API as the real `gh` sees it, from recorded answers: the replay the `gh` contract tests run against, and
the recorder that captures those answers from api.github.com.

`gh` talks plain http to `api.github.localhost` for the host `github.localhost` (its local-development host), and Go's
http client sends that through `HTTP_PROXY`. So with `GH_HOST=github.localhost` and `HTTP_PROXY` naming one of these
servers, every request `gh` makes arrives here: no certificate, no patched `gh`, no change to coxswain. The replay
answers each request from a recording and records what it could not answer. The recorder forwards each read to
api.github.com with a real token, refuses every write, and keeps the answer.

Every decision (the request key, what counts as a write, the scrub, the Link rewrite) is in `smoke/plan.py`.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from smoke import plan

UPSTREAM = "https://api.github.com"
UPSTREAM_TIMEOUT_S = 30


class _Handler(BaseHTTPRequestHandler):
    server: _GitHub

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, headers, payload = self.server.answer(self.command, self.path, body, dict(self.headers.items()))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

    def log_message(self, format: str, *args: object) -> None:
        pass


class _GitHub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.misses: list[str] = []

    @property
    def proxy(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def answer(self, method: str, target: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        raise NotImplementedError


class Replay(_GitHub):
    """Answers each request with the next recorded answer for its key; the last one repeats. A request with no
    recording gets a 599 and is listed in `misses`."""

    def __init__(self, exchanges: list[dict]) -> None:
        super().__init__()
        self.queues = plan.gh_exchange_queues(exchanges)

    def answer(self, method: str, target: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        key = plan.gh_request_key(method, target, body)
        with self.lock:
            queue = self.queues.get(key)
            if not queue:
                self.misses.append(key)
                return 599, {"Content-Type": "text/plain"}, f"no recorded answer for {key}".encode()
            exchange = queue.pop(0) if len(queue) > 1 else queue[0]
        return int(exchange["status"]), dict(exchange["headers"]), exchange["body"].encode("utf-8")


class Recorder(_GitHub):
    """Forwards each read to api.github.com with `token` and keeps the answer, scrubbed. Refuses every write with a
    599 and lists it in `misses`, so a recording never changes anything on GitHub; the one write it forwards is the
    deletion of a branch a GET has just shown is gone (`plan.gh_absent_branch_probe`), which GitHub refuses too."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token
        self.exchanges: list[dict] = []

    def _upstream(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        forward = {k: v for k, v in headers.items() if k.lower() in plan.GH_FORWARDED_HEADERS}
        request = urllib.request.Request(UPSTREAM + path, data=body or None, method=method,
                                         headers={**forward, "Authorization": f"token {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_S) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers.items()), exc.read()

    def answer(self, method: str, target: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        key = plan.gh_request_key(method, target, body)
        probe = plan.gh_absent_branch_probe(method, target)
        absent = probe is not None and self._upstream("GET", probe, b"", headers)[0] == 404
        if plan.gh_is_write(method, target, body) and not absent:
            with self.lock:
                self.misses.append(f"refused write: {key}")
            return 599, {"Content-Type": "text/plain"}, f"the recorder refuses writes: {key}".encode()
        status, got, raw = self._upstream(method, plan.gh_path(target), body, headers)
        exchange = plan.gh_exchange(key, status, got, raw.decode("utf-8", errors="replace"))
        with self.lock:
            self.exchanges.append(exchange)
        return status, dict(exchange["headers"]), exchange["body"].encode("utf-8")


@contextmanager
def serving(server: _GitHub) -> Iterator[_GitHub]:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, recording: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recording, indent=1, sort_keys=True) + "\n", encoding="utf-8")
