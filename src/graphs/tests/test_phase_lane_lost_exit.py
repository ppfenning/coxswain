"""A lane that lost its claim does not run on as nobody's (E1, 2026-11-21: two lanes whose first model call never
started kept running half an hour after another machine had re-claimed and finished their phases). Once its fence
loses the claim, against a real store, the lane has `LOST_EXIT_S` to wind down; past it, it kills its children and
exits there and then (`phase_lane.watch_lost`), whatever its threads are waiting on. A child that ignores TERM goes
with a KILL (`terminate_groups(..., sig=SIGKILL)`)."""

from __future__ import annotations

import signal
import subprocess
import threading
import time

import pytest

from harness.phase_lane import Fence, lost_overdue, terminate_groups, watch_lost
from harness.store_migrate import open_store
from tests.test_phase_lane_fence import AT, _claimed, _lapse, _until

DEAF = ["sh", "-c", "trap '' TERM; sleep 600"]  # ignores TERM, and its sleep inherits the ignore


@pytest.fixture
def lane_store(store_url, tmp_path):
    """A URL more than one connection reaches: sqlite's in-memory store is one connection's alone, so a file instead."""
    return f"sqlite:///{tmp_path / 'cox.db'}" if store_url.startswith("sqlite") else store_url


def test_a_lane_is_overdue_only_once_its_grace_since_the_loss_has_passed():
    assert not lost_overdue(None, now=1_000.0, grace_s=30.0)
    assert not lost_overdue(100.0, now=129.9, grace_s=30.0)
    assert lost_overdue(100.0, now=130.0, grace_s=30.0)


def test_a_child_that_ignores_term_outlives_the_stop_and_not_the_kill():
    child = subprocess.Popen(DEAF, start_new_session=True)
    try:
        time.sleep(0.2)
        terminate_groups([child.pid])
        time.sleep(0.3)
        assert child.poll() is None, "the TERM was not ignored: the test proves nothing"
        terminate_groups([child.pid], sig=signal.SIGKILL)
        assert _until(lambda: child.poll() is not None, 5)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_a_fence_that_loses_its_claim_ends_a_lane_that_does_not_end_within_its_grace(lane_store):
    owner = open_store(lane_store, AT)
    overdue: list[float] = []
    fence = Fence(lambda: open_store(lane_store, AT), _claimed(owner), lambda line: None, lambda: None, watch_s=0.05)
    ended = threading.Event()
    try:
        assert fence.check()
        fence.start()
        watch_lost(fence, ended, lambda: overdue.append(time.monotonic()), grace_s=0.5)
        time.sleep(0.8)
        assert overdue == [], "a lane that holds its claim is never ended"
        _lapse(owner)
        assert _until(fence.lost.is_set, 10)
        assert _until(lambda: overdue, 5), "the lane ran on past its grace with its claim lost"
        assert fence.lost_at is not None and overdue[0] - fence.lost_at >= 0.5
        time.sleep(0.3)
        assert len(overdue) == 1
    finally:
        ended.set()
        fence.stop()
        owner.close()


def test_a_lane_that_ends_in_time_is_left_alone(lane_store):
    owner = open_store(lane_store, AT)
    overdue: list[float] = []
    fence = Fence(lambda: open_store(lane_store, AT), _claimed(owner), lambda line: None, lambda: None, watch_s=0.05)
    ended = threading.Event()
    try:
        assert fence.check()
        watcher = watch_lost(fence, ended, lambda: overdue.append(time.monotonic()), grace_s=1.0)
        fence.lose("the claim is gone")
        ended.set()  # the lane wound down inside its grace
        watcher.join(timeout=5)
        assert not watcher.is_alive() and overdue == []
    finally:
        fence.stop()
        owner.close()
