import json
import threading

from harness.cli import _node_phase, _progress_observer
from harness.node_progress import Tracker

_OLD_KEYS = {
    "node", "attempt", "task", "tier", "status", "turns", "cost_usd",
    "started_at", "last_tool", "last_tool_at", "worktree", "branch",
}  # fmt: skip


def _tracker() -> Tracker:
    return Tracker(lambda: "2026-11-06T00:00:00Z")


def test_node_started_with_a_phase_carries_it():
    tracker = _tracker()
    tracker.start("build", 1, "t1", "premium", "/w/r/p1/t1", None, phase="p1")
    assert tracker.snapshot("r")["nodes"][0]["phase"] == "p1"


def test_node_started_without_a_phase_has_an_empty_one():
    tracker = _tracker()
    tracker.start("build", 1, "t1", "premium", "/w/r/p1/t1", None)
    assert tracker.snapshot("r")["nodes"][0]["phase"] == ""


def test_two_phases_yield_two_distinct_phases_in_one_snapshot():
    tracker = _tracker()
    tracker.start("build-a", 1, "t1", "premium", "/w/r/p1/t1", None, phase="p1")
    tracker.start("build-b", 1, "t2", "premium", "/w/r/p2/t2", None, phase="p2")
    nodes = tracker.snapshot("r")["nodes"]
    assert {n["phase"] for n in nodes} == {"p1", "p2"}


def test_existing_snapshot_keys_are_unchanged():
    tracker = _tracker()
    tracker.start("build", 1, "t1", "premium", "/w/r/p1/t1", None, phase="p1")
    snapshot = tracker.snapshot("r")
    assert set(snapshot) == {"version", "run", "written_at", "nodes"}
    assert snapshot["version"] == 1
    assert set(snapshot["nodes"][0]) == _OLD_KEYS | {"phase"}


def test_phase_comes_from_the_worktree_under_the_run_directory():
    assert _node_phase({"cwd": "/w/run1/p1/t1"}, "run1") == "p1"


def test_phase_is_empty_when_the_worktree_is_not_under_the_run_directory():
    assert _node_phase({"cwd": "/w/run1/t1"}, "run1") == ""
    assert _node_phase({"cwd": "/elsewhere/p1/t1"}, "run1") == ""


def test_phase_in_the_node_info_wins():
    assert _node_phase({"cwd": "/w/run1/p1/t1", "phase": "p9"}, "run1") == "p9"


def test_observer_writes_the_phase_into_the_progress_file(tmp_path):
    path = tmp_path / "progress.json"
    on_node = _progress_observer(_tracker(), "run1", path, threading.Lock(), traced=False)
    on_node("started", {"role": "build", "attempt": 1, "task": "t1", "tier": "premium", "cwd": "/w/run1/p1/t1"}, None)
    assert json.loads(path.read_text())["nodes"][0]["phase"] == "p1"
