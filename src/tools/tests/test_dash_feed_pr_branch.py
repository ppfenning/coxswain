import pytest

from agent_tools import dash_feed

MERGED = {"state": "approved", "extra": {"merged": True}}
APPROVED = {"state": "approved", "extra": {}}


def _row(task_id, phase="p1", needs=(), **fields):
    return {"kind": "task", "initiative": "a", "phase": phase, "task_id": task_id, "needs": list(needs), **fields}


@pytest.mark.parametrize(
    ("tasks", "expected"),
    [
        ([MERGED, MERGED], ("merged", None)),
        ([MERGED, {"state": "done"}], ("merged", None)),
        ([MERGED, {"state": "dropped"}], ("merged", None)),
        ([{"state": "done"}, {"state": "dropped"}], ("complete", None)),
        ([MERGED, {"task_id": "q", "state": "quarantined"}], ("blocked", "task q is quarantined")),
        ([MERGED, APPROVED], ("waiting", None)),
    ],
)
def test_phase_status_of_literal_task_rows(tasks, expected):
    assert dash_feed._phase_status(tasks, None, [], {}) == expected


@pytest.mark.parametrize("parent", ["merged", "complete"])
def test_a_child_of_a_finished_parent_is_not_waiting_on_it(parent):
    assert dash_feed._phase_status([{"state": "ready"}], None, ["p1"], {"p1": parent}) == ("waiting", None)


def test_a_child_of_a_running_parent_still_waits_on_it():
    status = dash_feed._phase_status([{"state": "ready"}], None, ["p1"], {"p1": "running"})
    assert status == ("waiting", "waiting on p1")


def test_waved_settles_a_merged_parent_then_its_child_without_a_wait_reason():
    rows = [_row("t1", "p1", **MERGED), _row("t2", "p2", ["t1"], state="ready")]
    row = {"initiative": "a", "phases": [{"id": "p1", "tasks": []}, {"id": "p2", "tasks": []}]}
    phases = dash_feed._waved(row, rows, {})["phases"]
    assert [(p["id"], p["status"], p["reason"]) for p in phases] == [("p1", "merged", None), ("p2", "waiting", None)]


def test_the_tally_separates_merged_from_approved():
    rows = [
        _row("t1", **MERGED), _row("t2", **MERGED), _row("t3", **APPROVED), _row("t4", state="done"),
        _row("t5", state="quarantined"),
    ]
    entries, tally = dash_feed._run_tasks(rows, [], {}, {}, "a")
    assert tally == {"landed": 1, "quarantined": 1, "merged": 2}
    assert [e["state"] for e in entries] == ["approved", "approved", "approved", "done", "quarantined"]
