import json
import os
from pathlib import Path

from harness.node_progress import Tracker, write_atomic


def _fixture() -> dict:
    src = next(p for p in Path(__file__).resolve().parents if p.name == "src")
    return json.loads((src / "tools" / "tests" / "fixtures" / "node_progress_v1.json").read_text())


def _assistant(*tools: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "x"}] + [{"type": "tool_use", "name": t} for t in tools]},
    }


class _Clock:
    def __init__(self, now: str) -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


def test_snapshot_equals_the_fixture():
    clock = _Clock("2026-10-30T14:00:00Z")
    tracker = Tracker(clock)
    tracker.start("plan", 1, "", "premium", "/tmp/worktrees/live-run-view-1/plan", None)
    for _ in range(8):
        tracker.observe("plan", 1, _assistant())
    clock.now = "2026-10-30T14:02:10Z"
    tracker.observe("plan", 1, _assistant("Read"))
    clock.now = "2026-10-30T14:02:40Z"
    tracker.observe("plan", 1, {"type": "result", "is_error": False, "total_cost_usd": 0.42})
    clock.now = "2026-10-30T14:03:00Z"
    tracker.start(
        "build",
        1,
        "p1-foundations-task",
        "standard",
        "/tmp/worktrees/live-run-view-1/build",
        "live-run-view-1/p1-foundations-task",
    )
    for _ in range(13):
        tracker.observe("build", 1, _assistant())
    clock.now = "2026-10-30T14:05:12Z"
    tracker.observe("build", 1, _assistant("Edit"))
    clock.now = "2026-10-30T14:05:30Z"
    assert tracker.snapshot("live-run-view-1") == _fixture()


def test_error_result_and_fail_mark_nodes_failed():
    tracker = Tracker(_Clock("2026-10-30T14:00:00Z"))
    tracker.start("a", 1, "", "standard", "/w/a", None)
    tracker.start("b", 1, "", "standard", "/w/b", None)
    tracker.observe("a", 1, {"type": "result", "is_error": True, "total_cost_usd": 0.1})
    tracker.fail("b", 1)
    by_node = {n["node"]: n for n in tracker.snapshot("r")["nodes"]}
    assert by_node["a"]["status"] == "failed"
    assert by_node["b"]["status"] == "failed"
    assert by_node["b"]["cost_usd"] is None


def test_second_attempt_is_its_own_entry():
    tracker = Tracker(_Clock("2026-10-30T14:00:00Z"))
    tracker.start("build", 1, "t", "standard", "/w/1", "br")
    tracker.observe("build", 1, _assistant("Edit"))
    tracker.fail("build", 1)
    tracker.start("build", 2, "t", "premium", "/w/2", "br")
    tracker.observe("build", 2, _assistant())
    first, second = tracker.snapshot("r")["nodes"]
    assert (first["attempt"], first["status"], first["turns"], first["last_tool"]) == (1, "failed", 1, "Edit")
    assert (second["attempt"], second["status"], second["turns"], second["last_tool"]) == (2, "running", 1, None)


def test_write_atomic_leaves_only_the_target(tmp_path):
    target = tmp_path / "progress.json"
    write_atomic(target, {"version": 1})
    write_atomic(target, {"version": 1, "run": "r"})
    assert json.loads(target.read_text()) == {"version": 1, "run": "r"}
    assert os.listdir(tmp_path) == ["progress.json"]
