from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness.node_progress import Tracker, write_atomic


def _fixture() -> dict:
    here = Path(__file__).resolve()
    src = next(p for p in here.parents if p.name == "src")
    path = src / "tools" / "tests" / "fixtures" / "node_progress_v1.json"
    return json.loads(path.read_text())


def _at(hms: str) -> datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(2026, 10, 30, h, m, s, tzinfo=UTC)


def _clock(*stamps: str):
    queue = [_at(s) for s in stamps]
    return lambda: queue.pop(0) if len(queue) > 1 else queue[0]


def _assistant(tool: str | None = None) -> dict:
    content = [{"type": "text", "text": "x"}]
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": {}})
    return {"type": "assistant", "message": {"content": content}}


def test_snapshot_equals_fixture() -> None:
    # Clock calls in order: plan start, plan tool_use, build start, build tool_use, snapshot.
    tracker = Tracker(_clock("14:00:00", "14:02:10", "14:03:00", "14:05:12", "14:05:30"))
    tracker.start("plan", 1, "", "premium", "/tmp/worktrees/live-run-view-1/plan", None)
    for _ in range(8):
        tracker.observe("plan", 1, _assistant())
    tracker.observe("plan", 1, _assistant("Read"))
    tracker.observe("plan", 1, {"type": "result", "is_error": False, "total_cost_usd": 0.42})
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
    tracker.observe("build", 1, _assistant("Edit"))
    assert tracker.snapshot("live-run-view-1") == _fixture()


def test_error_result_and_fail_mark_failed() -> None:
    tracker = Tracker(_clock("14:00:00"))
    tracker.start("a", 1, "", "standard", "/w/a", None)
    tracker.start("b", 1, "", "standard", "/w/b", None)
    tracker.observe("a", 1, _assistant("Read"))
    tracker.observe("a", 1, {"type": "result", "is_error": True})
    tracker.observe("b", 1, _assistant())
    tracker.fail("b", 1)
    nodes = {n["node"]: n for n in tracker.snapshot("r")["nodes"]}
    assert (nodes["a"]["status"], nodes["a"]["turns"], nodes["a"]["cost_usd"]) == ("failed", 1, None)
    assert (nodes["b"]["status"], nodes["b"]["turns"]) == ("failed", 1)


def test_second_attempt_is_a_separate_entry() -> None:
    tracker = Tracker(_clock("14:00:00"))
    tracker.start("build", 1, "t", "standard", "/w/1", "b1")
    tracker.observe("build", 1, _assistant())
    tracker.fail("build", 1)
    tracker.start("build", 2, "t", "premium", "/w/2", "b2")
    tracker.observe("build", 2, _assistant())
    tracker.observe("build", 2, _assistant())
    nodes = tracker.snapshot("r")["nodes"]
    assert [(n["attempt"], n["status"], n["turns"]) for n in nodes] == [
        (1, "failed", 1),
        (2, "running", 2),
    ]


def test_snapshot_is_a_copy_and_unknown_node_is_ignored() -> None:
    tracker = Tracker(_clock("14:00:00"))
    tracker.start("a", 1, "", "standard", "/w", None)
    tracker.observe("ghost", 1, _assistant("Read"))
    tracker.fail("ghost", 1)
    first = tracker.snapshot("r")
    first["nodes"][0]["turns"] = 99
    assert tracker.snapshot("r")["nodes"][0]["turns"] == 0
    assert len(first["nodes"]) == 1


def test_write_atomic_leaves_only_the_target(tmp_path: Path) -> None:
    target = tmp_path / "progress.json"
    write_atomic(target, {"version": 1, "nodes": []})
    write_atomic(target, {"version": 1, "nodes": [1]})
    assert json.loads(target.read_text()) == {"version": 1, "nodes": [1]}
    assert list(tmp_path.iterdir()) == [target]


def test_write_atomic_failure_leaves_no_temp_and_keeps_old_file(tmp_path: Path) -> None:
    target = tmp_path / "progress.json"
    write_atomic(target, {"ok": True})
    with pytest.raises(TypeError):
        write_atomic(target, {"bad": object()})
    assert json.loads(target.read_text()) == {"ok": True}
    assert list(tmp_path.iterdir()) == [target]
