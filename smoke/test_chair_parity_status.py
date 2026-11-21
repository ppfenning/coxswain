"""C6, end to end: `cox chair parity` (§5.3 step 3) and `cox chair status` (§5.4 step 5), against a real Postgres.

The C5 parity smoke's seed (something due for housekeeping, quarantine and decompose) goes into the sandbox's store,
which is then copied, as an operator restores a dump into a throwaway store. `cox chair parity --store-url <copy>`
runs the tick and every worker dry against the copy and reports a match; pointed at the profile's own store it
refuses, under its own URL or another port or query string onto it, and so it does with the live workspace. `cox chair status` with every kind listed names every lease and exits 1 while a listed kind has no worker;
with a real housekeeping worker running it shows that lease live, held by that worker. Nothing is stubbed.
"""

from __future__ import annotations

import contextlib
import socket
import socketserver
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import pytest

from smoke import plan
from smoke.test_chair_queue_parity import KINDS, _list, _seed

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def restored(sandbox, session_databases):
    """A copy of the sandbox's store under a name of the same session, dropped at the end."""
    _seed(sandbox)
    source = sandbox.store_url.rsplit("/", 1)[1].split("?")[0]
    name = f"{source[:58]}_copy"
    session_databases.copy(name, source)
    try:
        yield plan.with_database(sandbox.store_url, name)
    finally:
        session_databases.drop(name)


def test_parity_against_a_restored_copy_matches_kind_by_kind_and_refuses_the_live_store(sandbox, restored):
    done = sandbox.cox("chair", "parity", "--store-url", restored, timeout=900)
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert "parity: match" in out, out
    for kind in KINDS:
        assert f"worker {kind}: " in out and f"--kind {kind} --once --dry-run" in out, out
    alone = next(line for line in out.splitlines() if line.startswith("tick alone"))
    for planned in ("housekeeping", "request_triage", "launch_decompose"):
        assert planned in alone, out
    live = sandbox.cox("chair", "parity", "--store-url", sandbox.store_url, timeout=120)
    assert live.returncode == 2 and "refusing" in live.stdout, live.stdout + live.stderr
    ws = sandbox.cox("chair", "parity", "--store-url", restored, "--workspace", str(sandbox.workspace), timeout=120)
    assert ws.returncode == 2 and "is this profile's own workspace" in ws.stdout, ws.stdout + ws.stderr


def _with_query(url: str, query: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query=f"{parts.query}&{query}" if parts.query else query))


def _with_port(url: str, port: int) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{parts.netloc.rsplit(':', 1)[0]}:{port}"))


class Forward(socketserver.ThreadingTCPServer):
    """A second port onto the same Postgres server: every connection is relayed byte for byte."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, target: tuple[str, int]) -> None:
        self.target = target
        super().__init__(("127.0.0.1", 0), _Relay)


class _Relay(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = socket.create_connection(self.server.target)  # type: ignore[attr-defined]

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while data := src.recv(65536):
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    dst.shutdown(socket.SHUT_WR)

        back = threading.Thread(target=pump, args=(upstream, self.request), daemon=True)
        back.start()
        pump(self.request, upstream)
        back.join(timeout=30)
        upstream.close()


def test_parity_refuses_the_live_store_under_another_port_or_query_string(sandbox, restored):
    """C6: the live store is recognised by what it is (the server's system identifier and the database), not by its
    URL's text, so a second port onto the same server or an added query string is still refused; the restored copy,
    another database on the same server, is not."""
    parts = urlsplit(sandbox.store_url)
    forward = Forward((parts.hostname or "127.0.0.1", parts.port or 5432))
    threading.Thread(target=forward.serve_forever, daemon=True).start()
    try:
        for url in (_with_query(sandbox.store_url, "application_name=parity"),
                    _with_port(sandbox.store_url, forward.server_address[1])):
            assert url != sandbox.store_url
            done = sandbox.cox("chair", "parity", "--store-url", url, timeout=120)
            assert done.returncode == 2, (url, done.stdout + done.stderr)
            assert "is the store this profile uses (the same database on the same server)" in done.stdout, done.stdout
        moved = sandbox.cox("chair", "parity", "--store-url", _with_port(restored, forward.server_address[1]), timeout=900)
        assert moved.returncode == 0 and "parity: match" in moved.stdout, moved.stdout + moved.stderr
    finally:
        forward.shutdown()
        forward.server_close()


def test_status_names_every_lease_and_says_which_listed_kind_has_no_worker(sandbox):
    _list(sandbox, ", ".join(KINDS))
    none = sandbox.cox("chair", "status")
    assert none.returncode == 1, none.stdout + none.stderr
    assert "chair.queues: housekeeping, watch, quarantine, decompose, launch" in none.stdout
    assert all(f"chair:{kind} (listed): no lease row" in none.stdout for kind in KINDS), none.stdout
    assert "NOT RUNNING: housekeeping, watch, quarantine, decompose, launch" in none.stdout

    argv = [str(sandbox.install.cox), "chair", "worker", "--kind", "housekeeping", "--settle", "0"]
    worker = subprocess.Popen(argv, env=sandbox.env, cwd=sandbox.home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    threading.Thread(target=lambda: lines.extend(l.rstrip() for l in worker.stdout or []), daemon=True).start()
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not any("holds chair:housekeeping" in l for l in lines):
            time.sleep(0.1)
        assert any("holds chair:housekeeping" in l for l in lines), lines
        some = sandbox.cox("chair", "status")
    finally:
        worker.terminate()
        worker.wait(timeout=30)
    assert some.returncode == 1, some.stdout
    line = next(l for l in some.stdout.splitlines() if "chair:housekeeping (listed)" in l)
    assert ": live, held by housekeeping-" in line and "beat " in line and "expires in " in line, some.stdout
    assert "NOT RUNNING: watch, quarantine, decompose, launch" in some.stdout
