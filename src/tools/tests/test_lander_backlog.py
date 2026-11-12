"""The backlog manifest step: tasks one hand-folded pull request carried become done, once, and only from approved."""
import json

import pytest

from agent_tools import lander_backlog

MANIFEST = [
    {"task_id": "a", "initiative": "one", "status": "applied clean"},
    {"task_id": "b", "initiative": "one", "status": "applied, conflict resolved: imports"},
    {"task_id": "c", "initiative": "two", "status": "not applied: source commit is not in this clone or on origin"},
    {"task_id": "d", "initiative": "two", "status": "skipped"},
    {"task_id": "e", "initiative": "two", "status": "applied clean"},
]


def test_only_applied_tasks_that_are_approved_are_marked_and_a_second_run_changes_nothing() -> None:
    states = {("one", "a"): "approved", ("one", "b"): "approved", ("two", "c"): "approved", ("two", "d"): "approved", ("two", "e"): "ready"}
    marked: list[tuple] = []

    def mark_done(initiative: str, task: str, pr: str) -> None:
        marked.append((initiative, task, pr))
        states[(initiative, task)] = "done"

    first = lander_backlog.plan(MANIFEST, lambda i, t: states.get((i, t)))
    assert [(s.task, s.action) for s in first] == [("a", "done"), ("b", "done"), ("c", "skip"), ("d", "skip"), ("e", "skip")]
    assert lander_backlog.lines(first)[4] == "e: skipped: its state is ready, not approved"
    out = lander_backlog.apply(first, mark_done, "https://forge.example/pull/7")
    assert out[:2] == ["a: done", "b: done"] and marked == [("one", "a", "https://forge.example/pull/7"), ("one", "b", "https://forge.example/pull/7")]
    assert states[("two", "c")] == "approved", "a task whose commit was never applied is not on the parent"

    again = lander_backlog.plan(MANIFEST, lambda i, t: states.get((i, t)))
    assert [s.action for s in again] == ["skip"] * 5 and again[0].why == "already done"
    lander_backlog.apply(again, mark_done, "x")
    assert len(marked) == 2


def test_a_refused_mark_is_reported_and_the_manifest_must_be_readable(tmp_path) -> None:
    steps = lander_backlog.plan(MANIFEST[:1], lambda i, t: "approved")
    assert lander_backlog.apply(steps, lambda *a: "the store refused", "pr") == ["a: not marked done: the store refused"]
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"tasks": MANIFEST}))
    assert len(lander_backlog.read_manifest(path)) == 5
    with pytest.raises(ValueError, match="could not be read"):
        lander_backlog.read_manifest(tmp_path / "missing.json")
    path.write_text("{}")
    with pytest.raises(ValueError, match="has no tasks list"):
        lander_backlog.read_manifest(path)
