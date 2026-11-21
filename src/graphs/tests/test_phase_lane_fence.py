"""The lane's fence against a real store, sqlite and Postgres: it holds while the claim does, with the margin the claim's
own lease gives it. Once the claim is lost it stops the lane's children, then keeps stopping, until the lane is done,
each check or build started after the loss (a real process in a session of its own), but never a child in the lane's
own group, as a task thread's cleanup in the shared checkout is. On Postgres, a check that the store never answers
gives up within its bound (`phase_pick.CHECK_CALL_S`), so a silent store loses the claim rather than holding the
lane."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from harness import phase_pick, store_phases
from harness.phase_lane import Fence, LaneClaim, terminate_groups
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
    fence = Fence(lambda: open_store(relay.url(store_url), AT).limit(phase_pick.LANE_STATEMENT_S), _claimed(owner),
                  lines.append, lambda: lost.append(time.monotonic()))
    try:
        assert fence.check()
        relay.silent.set()
        started = time.monotonic()
        assert not fence.check()
        took = time.monotonic() - started
        assert phase_pick.CHECK_CALL_S - 0.5 < took < phase_pick.CHECK_CALL_S + 1.5, took
        assert lost and fence.lost.is_set()
        assert "the claim could not be read: StoreDeadline: the store did not answer within 3s" in fence.why, fence.why
        assert store_phases.held(owner, "ship", "p1", "w1", fence._claim.epoch)  # the store itself still says held
    finally:
        fence.stop()
        relay.close()
        owner.close()
