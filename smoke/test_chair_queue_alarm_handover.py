"""C6, end to end: with the tick stopped, a dead housekeeping worker is still reported, against a real Postgres.

Every kind is listed in `chair.queues` and no tick runs, as after the cutover. Two real `cox chair worker` processes
run: housekeeping and watch. Housekeeping is first in `QUEUE_KINDS`, so it raises the queue alarms for the kinds with
no worker, and the watch worker raises none. Then housekeeping is killed (SIGKILL: no release, its lease just stops
being renewed) and its lease's expiry is moved two minutes back in the store, which is what the clock would do in that
time. The watch worker's next pass raises `queue_unowned housekeeping`: it records the needs_chair under its own lease
and pushes it to the profile's ntfy URL, here a listener in the test. `cox chair watchdog --dry-run`, the timer's
check, names housekeeping too. Nothing is stubbed.
"""

from __future__ import annotations

import http.server
import subprocess
import threading
import time
from collections.abc import Callable
from typing import ClassVar

import pytest

psycopg = pytest.importorskip("psycopg")

KINDS = "housekeeping, watch, quarantine, decompose, launch"


class _Pushes(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.seen.append((self.headers.get("Title") or "", self.rfile.read(length).decode("utf-8")))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def ntfy():
    _Pushes.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pushes)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/cox", _Pushes.seen
    finally:
        server.shutdown()


class Worker:
    def __init__(self, sandbox, kind: str) -> None:
        argv = [str(sandbox.install.cox), "chair", "worker", "--kind", kind, "--poll", "2", "--min-gap", "0", "--settle", "0"]
        self.kind = kind
        self.proc = subprocess.Popen(argv, env=sandbox.env, cwd=sandbox.home, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        self.lines: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def wait_for(self, match: Callable[[str], bool], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = next((l for l in list(self.lines) if match(l)), None)
            if found is not None:
                return found
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        raise AssertionError(f"{self.kind}: no matching line in {timeout}s:\n" + "\n".join(self.lines))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=30)


def _unowned_rows(url: str) -> list[tuple[str, int, str]]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT action_json::text, epoch, holder FROM chair_actions WHERE kind = 'needs_chair' ORDER BY ts"
        ).fetchall()
    return [(text, epoch, holder) for text, epoch, holder in rows if "queue_unowned" in text]


def _causes(rows: list[tuple[str, int, str]]) -> list[str]:
    import json

    return [json.loads(text).get("cause", "") for text, _e, _h in rows]


def _pushed(pushes: list[tuple[str, str]], cause: str) -> bool:
    return any(cause in body for _title, body in pushes)


def _wait(done: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not done():
        time.sleep(0.2)


def _lease(url: str, name: str) -> tuple[str, int]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        holder, epoch = conn.execute("SELECT holder, epoch FROM leases WHERE name = %s", (name,)).fetchone()
    return holder, epoch


def test_killing_the_housekeeping_worker_still_raises_and_pushes_its_unowned_alarm(sandbox, ntfy):
    url, pushes = ntfy
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + f"chair:\n  queues: [{KINDS}]\nnotify: {{ntfy: {url}}}\n",
                       encoding="utf-8")
    housekeeping, watch = Worker(sandbox, "housekeeping"), Worker(sandbox, "watch")
    try:
        housekeeping.wait_for(lambda l: "holds chair:housekeeping" in l, 120)
        watch.wait_for(lambda l: "holds chair:watch" in l, 120)
        watch.wait_for(lambda l: l.startswith("chair worker watch 20"), 120)
        # Housekeeping watches while it runs: it reports the three kinds no one runs, and nobody reports watch or it.
        _wait(lambda: _pushed(pushes, "queue_unowned launch"), 60)
        before = _unowned_rows(sandbox.store_url)
        assert set(_causes(before)) == {"queue_unowned quarantine", "queue_unowned decompose", "queue_unowned launch"}, before
        assert not any("x needs_chair" in l for l in watch.lines if l.startswith("chair worker watch 20")), watch.lines

        housekeeping.proc.kill()
        housekeeping.proc.wait(timeout=30)
        # Fast-forward: the lease is no longer renewed, and two minutes on it has been dead past its time to live.
        with psycopg.connect(sandbox.store_url, connect_timeout=10, autocommit=True) as conn:
            conn.execute("UPDATE leases SET expires_at = to_char((now() AT TIME ZONE 'UTC') - interval '2 minutes', "
                         "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') WHERE name = 'chair:housekeeping'")
        line = watch.wait_for(lambda l: l.startswith("chair worker watch 20") and "x needs_chair" in l, 60)
        assert "lease lost" not in line, line
        _wait(lambda: _pushed(pushes, "queue_unowned housekeeping"), 30)
    finally:
        housekeeping.stop()
        watch.stop()
    _holder, watch_epoch = _lease(sandbox.store_url, "chair:watch")
    after = [row for row in _unowned_rows(sandbox.store_url) if "queue_unowned housekeeping" in row[0]]
    assert after and all(epoch == watch_epoch for _t, epoch, _h in after), (after, watch.lines)
    assert _pushed(pushes, "queue_unowned housekeeping"), pushes
    # The timer's check, the line behind the workers, names it too (and the dead kinds no one ever ran).
    done = sandbox.cox("chair", "watchdog", "--dry-run", "--stale-minutes", "1", timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Chair queue down" in done.stdout and "housekeeping" in done.stdout, done.stdout
    assert "Chair loop down" not in done.stdout, done.stdout  # the stopped tick is not judged with every kind listed
