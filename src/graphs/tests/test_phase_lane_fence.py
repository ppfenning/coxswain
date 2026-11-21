"""The lane's fence against a real store, sqlite and Postgres: it holds while the claim does, with the margin the claim's
own lease gives it. Once the claim is lost it stops the lane's children, then keeps stopping, until the lane is done,
each check or build started after the loss (a real process in a session of its own), but never a child in the lane's
own group, as a task thread's cleanup in the shared checkout is. On Postgres, a check that the store never answers
gives up within its bound (`phase_pick.CHECK_CALL_S`), so a silent store loses the claim rather than holding the
lane, while a store busy with a table lock the check waits on (a migration's ALTER TABLE) answers it with the server's
error first (`phase_pick.CHECK_SERVER_MS`), and the lane holds on, on the lease it read last, until that is within its
margin."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from harness import phase_pick, store_phases
from harness.phase_lane import Fence, LaneClaim, terminate_groups
from harness.store_dialect import connect
from harness.store_migrate import open_store
from tests.test_store_deadline import SilentRelay

AT = "2026-11-20T00:00:00Z"
SLEEP = [sys.executable, "-c", "import time; time.sleep(600)"]


@pytest.fixture
def lane_store(store_url, tmp_path):
    """A URL more than one connection reaches: sqlite's in-memory store is one connection's alone, so a file instead."""
    return f"sqlite:///{tmp_path / 'cox.db'}" if store_url.startswith("sqlite") else store_url


def _until(check, within_s: float) -> bool:
    deadline = time.monotonic() + within_s
    while not check():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _claimed(owner, lease_s: int = phase_pick.MIN_LEASE_S):
    store_phases.apply_changes(owner, [{"op": "insert", "initiative": "ship", "phase": "p1", "requires": []}], "chair")
    claim = store_phases.claim_next(owner, "w1", [], 1, lease_s=lease_s)
    assert claim is not None
    return LaneClaim("ship", "p1", "w1", claim.epoch)


def _lapse(owner) -> None:
    owner.execute("UPDATE phase_queue SET lease_until = '2000-01-01T00:00:00Z' WHERE phase = 'p1'")


def test_a_lost_fence_stops_checks_started_after_the_loss_and_never_the_lane_s_own_cleanup(lane_store):
    owner = open_store(lane_store, AT)
    children: list[subprocess.Popen[bytes]] = []
    stops: list[float] = []

    def pids() -> list[int]:
        return [p.pid for p in children if p.poll() is None]

    def on_lost() -> None:
        stops.append(time.monotonic())
        terminate_groups(pids())

    fence = Fence(lambda: open_store(lane_store, AT), _claimed(owner), lambda line: None, on_lost, watch_s=0.05,
                  still_lost=lambda: terminate_groups(pids(), groups_only=True))
    try:
        assert fence.check()  # 48 s left, more than the 12 s margin its own lease gives the lane
        fence.start()
        time.sleep(0.3)
        assert stops == [] and not fence.lost.is_set()
        _lapse(owner)
        assert _until(fence.lost.is_set, 10) and len(stops) == 1
        # After the loss: a check a task thread starts (a session of its own), and its cleanup's git (the lane's group).
        check = subprocess.Popen(SLEEP, start_new_session=True)
        cleanup = subprocess.Popen(SLEEP)
        children += [check, cleanup]
        assert _until(lambda: check.poll() is not None, 10), "the check started after the loss runs on"
        time.sleep(0.3)  # several more passes of the fence's watch
        assert cleanup.poll() is None, "the repeat stopped the lane's own cleanup"
        assert len(stops) == 1  # the full stop ran once, at the loss; the repeats stop only new groups
        fence.stop()
        late = subprocess.Popen(SLEEP, start_new_session=True)
        children.append(late)
        time.sleep(0.3)
        assert late.poll() is None  # done: nothing more is stopped
        assert not [t for t in threading.enumerate() if t.name == "lane-fence"]
    finally:
        fence.stop()
        for p in children:
            if p.poll() is None:
                p.kill()
            p.wait()
        owner.close()


@pytest.fixture
def pg_store(store_url):
    """The Postgres store alone (sqlite has no socket to fall silent): `store_url`'s schema, through a relay of the
    test's own. Skips, as `store_url` does, when COXSWAIN_TEST_PG_URL is not set."""
    if not os.environ.get("COXSWAIN_TEST_PG_URL"):
        pytest.skip("COXSWAIN_TEST_PG_URL is not set")
    return store_url


@pytest.mark.parametrize("store_url", ["postgres"], indirect=True)
def test_a_check_the_store_never_answers_loses_the_claim_within_its_bound(pg_store):
    store_url = pg_store
    owner = open_store(store_url, AT)
    relay = SilentRelay(store_url)
    lost: list[float] = []
    lines: list[str] = []
    claim = _claimed(owner)
    # The lane's write connection, through the same relay: a write blocked on the silent store when the check gives up.
    writes = open_store(relay.url(store_url), AT).bound_socket(phase_pick.LANE_SOCKET_S)
    abandoned: list[str] = []

    def on_silent(why: str) -> None:
        abandoned.append(why)
        writes.abandon(why)

    fence = Fence(lambda: open_store(relay.url(store_url), AT), claim, lines.append,
                  lambda: lost.append(time.monotonic()), on_silent=on_silent)
    blocked: list[BaseException] = []

    def write() -> None:
        try:
            writes.query_one("SELECT 1")
        except BaseException as exc:  # the test reads which
            blocked.append(exc)

    try:
        assert fence.check()
        relay.silent.set()
        writer = threading.Thread(target=write)
        writer.start()
        started = time.monotonic()
        assert not fence.check()
        took = time.monotonic() - started
        assert phase_pick.CHECK_CALL_S - 0.5 < took < phase_pick.CHECK_CALL_S + 1.5, took
        assert lost and fence.lost.is_set()
        assert "the claim could not be read: StoreDeadline: the store did not answer within 3s" in fence.why, fence.why
        writer.join(timeout=2)  # abandoned: the blocked write fails at once, nothing of the lane's waits on the store
        assert not writer.is_alive() and abandoned == [fence.why]
        assert [type(e).__name__ for e in blocked] == ["StoreDeadline"], blocked
        assert store_phases.held(owner, "ship", "p1", "w1", fence._claim.epoch)  # the store itself still says held
    finally:
        fence.stop()
        relay.close()
        writes.close()
        owner.close()


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_a_busy_store_holds_the_claim_on_the_lease_read_last_until_it_is_within_its_margin(lane_store, monkeypatch):
    owner = open_store(lane_store, AT)
    clock = _Clock()
    lost: list[str] = []
    lines: list[str] = []
    fence = Fence(lambda: open_store(lane_store, AT), _claimed(owner), lines.append, lambda: lost.append("x"),
                  monotonic=clock)
    try:
        assert fence.check()  # 48 s left of a 48 s lease, a 12 s margin
        real = store_phases.lease_left

        def busy(*_a, **_k):
            raise RuntimeError("canceling statement due to lock timeout")

        monkeypatch.setattr(store_phases, "lease_left", busy)
        clock.t += 30  # 18 s left by the lane's own clock: busy, and held
        assert fence.check() and not fence.lost.is_set() and not lost
        assert "lane: the store is busy (RuntimeError: canceling statement due to lock timeout)" in lines[-1], lines
        monkeypatch.setattr(store_phases, "lease_left", real)
        assert fence.check()  # the store answers again: the lease read anew
        monkeypatch.setattr(store_phases, "lease_left", busy)
        clock.t += 37  # 11 s (10 across a second) left of the lease read last: within the margin
        assert not fence.check() and fence.lost.is_set() and lost == ["x"]
        assert "s left, within its 12s margin" in fence.why, fence.why
    finally:
        fence.stop()
        owner.close()


def test_a_busy_first_check_has_no_lease_to_go_on_and_loses_the_claim(lane_store, monkeypatch):
    owner = open_store(lane_store, AT)
    fence = Fence(lambda: open_store(lane_store, AT), _claimed(owner), lambda line: None, lambda: None)

    def busy(*_a, **_k):
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(store_phases, "lease_left", busy)
    try:
        assert not fence.check()
        assert "no earlier check read its lease" in fence.why, fence.why
    finally:
        fence.stop()
        owner.close()


@pytest.mark.parametrize("store_url", ["postgres"], indirect=True)
def test_a_table_lock_held_past_the_check_s_bound_never_loses_a_live_lane_or_counts_an_attempt(pg_store):
    """The reviewer's case: a migration's ACCESS EXCLUSIVE lock on phase_queue held 5 s, past `CHECK_CALL_S`. The
    check's server bounds end its wait first, with an error; the lane checks again on the lease it read, and holds."""
    owner = open_store(pg_store, AT)
    claim = _claimed(owner)
    attempts = store_phases.get_row(owner, "ship", "p1")["attempts"]
    lines: list[str] = []
    lost: list[float] = []
    silent: list[str] = []
    fence = Fence(lambda: open_store(pg_store, AT), claim, lines.append, lambda: lost.append(time.monotonic()),
                  watch_s=0.2, on_silent=silent.append)
    locker = connect(pg_store)
    locked = threading.Event()

    def hold() -> None:
        with locker.transaction():
            locker.execute("LOCK TABLE phase_queue IN ACCESS EXCLUSIVE MODE")
            locked.set()
            time.sleep(5)

    try:
        assert fence.check()
        holder = threading.Thread(target=hold)
        holder.start()
        assert locked.wait(5)
        started = time.monotonic()
        assert fence.check()  # answered with the server's error inside the client's bound, not given up
        assert time.monotonic() - started < phase_pick.CHECK_CALL_S
        fence.start()  # the watch's checks through the rest of the lock, and a check of the lane's beside them
        assert fence.check()
        holder.join(timeout=10)
        time.sleep(0.6)  # the watch's checks after the lock: answered again
        assert not fence.lost.is_set() and not lost and not silent, (fence.why, lines)
        assert any("lane: the store is busy" in line for line in lines), lines
        assert fence.check()
        row = store_phases.get_row(owner, "ship", "p1")
        assert (row["state"], row["claimed_by"], row["claim_epoch"]) == ("claimed", "w1", claim.epoch)
        assert row["attempts"] == attempts  # nothing lost, so nothing to count
    finally:
        fence.stop()
        locker.close()
        owner.close()


@pytest.mark.parametrize("store_url", ["postgres"], indirect=True)
def test_a_store_silent_when_the_fence_reconnects_loses_the_claim_within_its_bound(pg_store):
    store_url = pg_store
    owner = open_store(store_url, AT)
    relay = SilentRelay(store_url)
    claim = _claimed(owner)
    fence = Fence(lambda: open_store(relay.url(store_url), AT), claim, lambda line: None, lambda: None)
    try:
        relay.silent.set()
        started = time.monotonic()
        assert not fence.check()
        assert time.monotonic() - started < phase_pick.CHECK_CALL_S + 1.0
        assert "StoreDeadline: the store did not answer an open within 3s" in fence.why, fence.why
    finally:
        fence.stop()
        relay.close()
        owner.close()
