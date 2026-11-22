"""A lane that lost its claim does not run on as nobody's (E1, 2026-11-21: two lanes whose first model call never
started kept running half an hour after another machine had re-claimed and finished their phases). Once its fence
loses the claim, against a real store, the lane has `LOST_EXIT_S` to wind down; past it, it kills its children and
exits there and then (`phase_lane.LostWatch`), whatever its threads are waiting on; a child that ignores TERM goes
with a KILL (`terminate_groups(..., sig=SIGKILL)`). Only its children that lead groups of their own (checks, agents)
are killed, never its own git in the shared checkout, and once its cleanup starts nothing is."""

from __future__ import annotations

import signal
import subprocess
import time

import pytest

from harness.phase_lane import Fence, LostWatch, lost_overdue, terminate_groups
from harness.store_migrate import open_store
from tests.test_phase_lane_fence import AT, SLEEP, _claimed, _lapse, _until

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


def _children() -> tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]]:
    """A lane's two kinds of child: a check, deaf to TERM, in a session of its own, and a git of its own cleanup in the
    shared checkout, in the lane's group (here the test's)."""
    return subprocess.Popen(DEAF, start_new_session=True), subprocess.Popen(SLEEP)


def _reap(*children: subprocess.Popen[bytes]) -> None:
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_a_lane_still_at_its_work_past_its_grace_kills_its_checks_and_never_its_own_git(lane_store):
    owner = open_store(lane_store, AT)
    check, git = _children()
    overdue: list[float] = []

    def kill() -> None:  # the lane's `overdue`, less the exit: `cli_run_graph`'s `kill_children`
        overdue.append(time.monotonic())
        terminate_groups([check.pid, git.pid], groups_only=True, sig=signal.SIGKILL)

    fence = Fence(lambda: open_store(lane_store, AT), _claimed(owner), lambda line: None, lambda: None, watch_s=0.05)
    watch = None
    try:
        assert fence.check()
        fence.start()
        watch = LostWatch(fence, kill, grace_s=0.5)
        time.sleep(0.8)
        assert overdue == [], "a lane that holds its claim is never ended"
        _lapse(owner)
        assert _until(fence.lost.is_set, 10)
        assert _until(lambda: overdue, 5), "the lane ran on past its grace with its claim lost"
        assert fence.lost_at is not None and overdue[0] - fence.lost_at >= 0.5
        assert _until(lambda: check.poll() is not None, 5), "the deaf check outlived the kill"
        time.sleep(0.3)
        assert git.poll() is None, "the kill reached the lane's own git in the shared checkout"
        assert len(overdue) == 1
    finally:
        if watch is not None:
            watch.disarm()
        fence.stop()
        owner.close()
        _reap(check, git)


def test_a_slow_cleanup_that_starts_inside_the_grace_is_never_killed(lane_store):
    owner = open_store(lane_store, AT)
    check, git = _children()
    overdue: list[float] = []
    fence = Fence(lambda: open_store(lane_store, AT), _claimed(owner), lambda line: None, lambda: None, watch_s=0.05)
    try:
        assert fence.check()
        watch = LostWatch(fence, lambda: overdue.append(time.monotonic()), grace_s=0.5)
        fence.lose("the claim is gone")
        watch.disarm()  # the lane's `finally` starts: its cleanup (`git`) runs on well past the grace
        time.sleep(1.5)
        watch.thread.join(timeout=5)
        assert not watch.thread.is_alive() and overdue == []
        assert git.poll() is None and check.poll() is None
    finally:
        fence.stop()
        owner.close()
        _reap(check, git)
