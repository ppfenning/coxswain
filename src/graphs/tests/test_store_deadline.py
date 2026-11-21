"""A store call bounded on the client's side (`store_dialect.Connection.limit`, `.within`): a store gone silent, its
socket open and every byte dropped, cannot hold a caller past its deadline, whatever the kernel would wait. Postgres
only, through a relay of the test's (`SilentRelay`); sqlite has no socket, and the bounds leave it as it is."""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import pytest

from harness.store_dialect import Connection, StoreDeadline, connect, tcp_options


class SilentRelay:
    """A TCP relay to a Postgres server on a loopback port of its own. `silence` keeps every socket open and drops
    every byte from then on, both ways, as a store behind a dead network path does: nothing is refused or reset."""

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        self._target = (parts.hostname or "127.0.0.1", parts.port or 5432)
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self.silent = threading.Event()
        self._open: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def url(self, url: str) -> str:
        parts = urlsplit(url)
        userinfo = parts.netloc.rpartition("@")[0]
        return urlunsplit(parts._replace(netloc=f"{userinfo}@127.0.0.1:{self.port}" if userinfo else f"127.0.0.1:{self.port}"))

    def _accept(self) -> None:
        with contextlib.suppress(OSError):
            while True:
                near, _ = self._server.accept()
                far = socket.create_connection(self._target, timeout=10)
                far.settimeout(None)
                self._open += [near, far]
                for src, dst in ((near, far), (far, near)):
                    threading.Thread(target=self._pump, args=(src, dst), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while data := src.recv(65536):
                if not self.silent.is_set():
                    dst.sendall(data)

    def close(self) -> None:
        self._server.close()
        for s in self._open:
            with contextlib.suppress(OSError):
                s.close()


@pytest.fixture
def pg_url():
    base = os.environ.get("COXSWAIN_TEST_PG_URL")
    if not base:
        pytest.skip("COXSWAIN_TEST_PG_URL is not set")
    return base


@pytest.fixture
def relay(pg_url):
    r = SilentRelay(pg_url)
    yield r
    r.close()


def _timed(conn: Connection, sql: str) -> tuple[float, BaseException | None]:
    started = time.monotonic()
    try:
        conn.query_one(sql)
    except BaseException as exc:
        return time.monotonic() - started, exc
    return time.monotonic() - started, None


def test_a_statement_on_a_silent_store_ends_at_its_limit_and_the_connection_is_given_up(pg_url, relay):
    conn = connect(relay.url(pg_url)).limit(1.5)
    try:
        assert conn.query_one("SELECT 1") == (1,)
        relay.silent.set()
        took, exc = _timed(conn, "SELECT 1")
        assert isinstance(exc, StoreDeadline) and "did not answer within 1.5s" in str(exc), exc
        assert 1.4 < took < 3.0, took
        took, exc = _timed(conn, "SELECT 1")  # dead from then on: raises at once, never waits again
        assert isinstance(exc, StoreDeadline) and "given up" in str(exc) and took < 0.5, (took, exc)
    finally:
        conn.close()


def test_a_transaction_on_a_silent_store_ends_at_its_call_s_deadline(pg_url, relay):
    conn = connect(relay.url(pg_url))
    try:
        started = time.monotonic()
        with pytest.raises(StoreDeadline), conn.within(2.0), conn.transaction():
            conn.query_one("SELECT 1")
            relay.silent.set()
            conn.query_one("SELECT 2")
            conn.query_one("SELECT 3")
        assert time.monotonic() - started < 3.5
    finally:
        conn.close()


def test_a_call_s_deadline_covers_its_statements_together_and_ends_with_its_block(pg_url):
    conn = connect(pg_url)
    try:
        with conn.within(1.5):
            assert conn.query_one("SELECT 1") == (1,)
            took, exc = _timed(conn, "SELECT pg_sleep(0.9)")
            assert exc is None, exc
            took, exc = _timed(conn, "SELECT pg_sleep(5)")  # 0.6 s left of the call: the deadline, not the sleep
        assert isinstance(exc, StoreDeadline) and took < 1.5, (took, exc)
        fresh = connect(pg_url)
        try:
            with fresh.within(0.5):
                fresh.query_one("SELECT 1")
            assert fresh.query_one("SELECT pg_sleep(1)") is not None  # out of the block: no deadline left over
        finally:
            fresh.close()
    finally:
        conn.close()


def test_a_live_store_answers_well_inside_the_bounds_and_the_socket_carries_them(pg_url):
    conn = connect(pg_url).limit(6)
    try:
        for _ in range(20):
            assert conn.query_one("SELECT 1") == (1,)
        with conn.within(3), conn.transaction():
            conn.query_one("SELECT 1")
        fd = conn._fileno()
        assert fd is not None
        probe = socket.socket(fileno=os.dup(fd))
        try:
            assert probe.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
            if hasattr(socket, "TCP_USER_TIMEOUT"):
                assert probe.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 6000
        finally:
            probe.close()
    finally:
        conn.close()


def test_the_tcp_options_bound_silence_to_about_the_deadline_where_the_kernel_can():
    found = {(level, option): value for level, option, value in tcp_options(10)}
    assert found[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1
    if hasattr(socket, "TCP_USER_TIMEOUT"):
        assert found[(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT)] == 10_000
    if hasattr(socket, "TCP_KEEPCNT") and hasattr(socket, "TCP_KEEPINTVL"):
        assert found[(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)] * found[(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)] <= 5


def test_sqlite_has_no_socket_and_the_bounds_leave_it_as_it_is(tmp_path):
    conn = connect(f"sqlite:///{tmp_path / 'cox.db'}").limit(0.01)
    try:
        with conn.within(0.01):
            time.sleep(0.05)
            assert conn.query_one("SELECT 1") == (1,)  # no deadline: sqlite waits on its file lock, as before
    finally:
        conn.close()


def test_a_lane_s_socket_bound_never_cuts_a_lock_wait_on_a_live_store(pg_url):
    """The lane's write connections carry only the socket's bound: a statement waiting on a lock a live store holds
    waits it out, however long past that bound, rather than being taken for a store gone silent."""
    holder, waiter = connect(pg_url), connect(pg_url).bound_socket(1)
    try:
        holder.execute("SELECT pg_advisory_lock(424242)")
        release = threading.Timer(2.5, lambda: holder.execute("SELECT pg_advisory_unlock(424242)"))
        release.start()
        started = time.monotonic()
        assert waiter.query_one("SELECT pg_advisory_lock(424242)") is not None
        assert time.monotonic() - started > 2.0
        waiter.execute("SELECT pg_advisory_unlock(424242)")
        release.join()
    finally:
        holder.close()
        waiter.close()


def test_an_abandoned_connection_fails_a_blocked_statement_at_once_and_every_later_one(pg_url):
    conn = connect(pg_url)
    try:
        failed: list[BaseException] = []

        def sleep() -> None:
            try:
                conn.query_one("SELECT pg_sleep(30)")
            except BaseException as exc:  # the test reads which
                failed.append(exc)

        sleeper = threading.Thread(target=sleep)
        sleeper.start()
        time.sleep(0.5)
        conn.abandon("the fence found the store silent")
        sleeper.join(timeout=3)
        assert not sleeper.is_alive() and [type(e) for e in failed] == [StoreDeadline], failed
        took, exc = _timed(conn, "SELECT 1")
        assert isinstance(exc, StoreDeadline) and "the fence found the store silent" in str(exc) and took < 0.5
    finally:
        conn.close()
