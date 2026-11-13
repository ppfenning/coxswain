import json
from datetime import UTC, datetime

from agent_tools.completions import completions, count_completions


def _row(task, updated_at="2026-11-09T10:00:00Z", run="acme-1", **record):
    return {"run_id": run, "phase_id": "p1", "task_id": task, "record_json": json.dumps(record), "updated_at": updated_at}


def _at(hour):
    return datetime(2026, 11, 9, hour, tzinfo=UTC)


def test_three_tasks_merged_into_the_pr_branch_and_nothing_on_main_are_three_completions():
    rows = [_row(t, status="approved", merged=True, merged_commit="abc") for t in ("t1", "t2", "t3")]
    assert len(completions(rows)) == 3


def test_a_task_merged_and_later_landed_on_main_counts_once_at_the_earlier_stamp():
    row = _row("t1", updated_at="2026-11-09T12:00:00Z", status="done", merged=True, landed={"at": "2026-11-09T11:00:00Z", "pr": 7})
    assert completions([row]) == [("acme-1", "t1", _at(11))]


def test_a_task_approved_but_not_merged_does_not_count():
    assert completions([_row("t1", status="approved", merged=False)]) == []


def test_a_done_task_counts_at_its_updated_at():
    assert completions([_row("t1", status="done")]) == [("acme-1", "t1", _at(10))]


def test_a_landed_task_with_a_stamp_counts_without_a_done_status():
    assert completions([_row("t1", status="approved", landed={"at": "2026-11-09T09:00:00Z"})]) == [("acme-1", "t1", _at(9))]


def test_a_record_with_no_usable_stamp_is_skipped_not_raised():
    rows = [
        _row("t1", updated_at=None, status="done"),
        _row("t2", updated_at="not a time", status="done"),
        _row("t3", status="approved", landed=True),
        {"run_id": "acme-1", "task_id": "t4", "record_json": "{not json", "updated_at": "2026-11-09T10:00:00Z"},
        {"run_id": "acme-1", "task_id": "t5"},
        {"task_id": "t6", "record_json": {"status": "done"}, "updated_at": "2026-11-09T10:00:00Z"},
    ]
    assert completions(rows) == []


def test_a_decoded_record_dict_and_a_naive_stamp_read_as_utc():
    row = {"run_id": "acme-1", "task_id": "t1", "record_json": {"status": "done"}, "updated_at": "2026-11-09T10:00:00"}
    assert completions([row]) == [("acme-1", "t1", _at(10))]


def test_count_includes_the_window_start_and_excludes_earlier_completions():
    done = [("r", "t1", _at(9)), ("r", "t2", _at(10)), ("r", "t3", _at(11))]
    assert count_completions(done, _at(10)) == 2
