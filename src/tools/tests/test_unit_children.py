"""`unit_children`: what a chair stops of its own children on exit, now that its units stop only the main process."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from agent_tools import unit_children
from agent_tools.unit_children import Proc, Target

ME, GROUP = 100, 100


def test_a_direct_child_is_stopped_with_its_group_and_a_kept_one_is_left_with_its_group():
    procs = [
        Proc(ME, 1, GROUP),
        Proc(200, ME, 200), Proc(201, 200, 200),  # a check in its own session, and its pytest
        Proc(300, ME, 300), Proc(301, 300, 300),  # a land, marked detached, and the land's own process
        Proc(400, ME, GROUP),  # a git call in the chair's own group
    ]
    assert unit_children.targets(procs, ME, GROUP, frozenset({300})) == [Target(200, True), Target(400, False)]


def test_a_grandchild_left_in_the_chairs_own_group_is_stopped_only_when_the_chair_leads_that_group():
    procs = [Proc(ME, 1, GROUP), Proc(500, 1, GROUP), Proc(600, 1, 999)]
    assert unit_children.targets(procs, ME, GROUP, frozenset()) == [Target(500, False)]
    assert unit_children.targets([Proc(ME, 1, 999), Proc(500, 1, 999)], ME, 999, frozenset()) == []


def test_parse_ps_drops_lines_that_are_not_three_numbers():
    assert unit_children.parse_ps("  1   0   1\nPID PPID PGID\n 7 1 7\n\n") == [Proc(1, 0, 1), Proc(7, 1, 7)]


def test_a_process_that_is_not_a_session_leader_stops_nothing():
    assert unit_children.stop_children(ps=lambda: f"9999 {os.getpid()} 9999\n", leader=lambda: False) == ()


def _gone(pid: int) -> bool:
    """`stop_children` reaps a direct child it stopped, so `Popen.wait` can no longer read its status: ask the kernel."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)


def test_on_exit_a_check_is_stopped_and_a_detached_land_keeps_running():
    check, land = _sleeper(), _sleeper()
    unit_children.detached(land.pid)
    me = os.getpid()
    try:
        # The real listing, cut to these two, so the test process's other children are never touched.
        listing = lambda: "".join(f"{p} {me} {p}\n" for p in (check.pid, land.pid))  # noqa: E731
        lines: list[str] = []
        stopped = unit_children.stop_children(grace=5.0, ps=listing, log=lines.append, leader=lambda: True)
        assert stopped == [Target(check.pid, True)]
        assert _gone(check.pid)
        time.sleep(0.2)
        assert land.poll() is None
        assert lines and str(check.pid) in lines[0]
    finally:
        for proc in (check, land):
            proc.kill()
            proc.wait()


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace():
    stubborn = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"],
        start_new_session=True, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert stubborn.stdout is not None and stubborn.stdout.readline().strip() == "ready"
        me = os.getpid()
        unit_children.stop_children(grace=0.5, ps=lambda: f"{stubborn.pid} {me} {stubborn.pid}\n", leader=lambda: True)
        assert _gone(stubborn.pid)
    finally:
        stubborn.kill()
        stubborn.wait()
