import pytest

from harness.store_read import work_items
from harness.store_write import record_handoff, record_merge, upsert_work_item

T1 = "2026-11-03T01:00:00Z"
T2 = "2026-11-03T02:00:00Z"
HANDOFF = {"summary": "done", "files": ["a.py", "b.py"]}


def seed(conn, task="t1", state="approved"):
    upsert_work_item(conn, "i1", task, "p1", state, ["t0"], T1, "seed")


def row(conn, task="t1"):
    return next(r for r in work_items(conn, "i1") if r["task_id"] == task)


def test_a_fresh_row_reads_unmerged_with_no_handoff(store_conn):
    seed(store_conn)
    r = row(store_conn)
    assert (r["merged"], r["merged_commit"], r["handoff"]) == (False, None, None)
    assert r["needs"] == ["t0"]


def test_record_merge_on_an_approved_row_sets_the_commit(store_conn):
    seed(store_conn)
    assert record_merge(store_conn, "i1", "t1", "abc123", T2, "merger") == 1
    r = row(store_conn)
    assert (r["merged"], r["merged_commit"], r["updated_at"], r["updated_by"]) == (True, "abc123", T2, "merger")
    assert r["state"] == "approved"


@pytest.mark.parametrize("state", ["open", "done"])
def test_record_merge_refuses_a_row_that_is_not_approved(store_conn, state):
    seed(store_conn, state=state)
    with pytest.raises(ValueError, match="not 'approved'"):
        record_merge(store_conn, "i1", "t1", "abc123", T2, "merger")
    r = row(store_conn)
    assert (r["merged"], r["merged_commit"], r["updated_at"], r["updated_by"]) == (False, None, T1, "seed")


def test_record_handoff_round_trips_a_record_and_leaves_state_alone(store_conn):
    seed(store_conn, state="open")
    assert record_handoff(store_conn, "i1", "t1", HANDOFF, T2, "builder") == 1
    r = row(store_conn)
    assert (r["handoff"], r["state"], r["updated_at"], r["updated_by"]) == (HANDOFF, "open", T2, "builder")


def test_neither_writer_inserts_a_missing_row(store_conn):
    assert record_merge(store_conn, "i1", "nope", "abc123", T2, "merger") == 0
    assert record_handoff(store_conn, "i1", "nope", HANDOFF, T2, "builder") == 0
    assert work_items(store_conn, "i1") == []


def test_repeating_record_merge_with_the_same_commit_changes_nothing(store_conn):
    seed(store_conn)
    record_merge(store_conn, "i1", "t1", "abc123", T1, "merger")
    before = row(store_conn)
    assert record_merge(store_conn, "i1", "t1", "abc123", T2, "someone") == 0
    assert row(store_conn) == before


def test_repeating_record_handoff_with_the_same_record_changes_nothing(store_conn):
    seed(store_conn)
    record_handoff(store_conn, "i1", "t1", HANDOFF, T1, "builder")
    before = row(store_conn)
    assert record_handoff(store_conn, "i1", "t1", {"files": ["a.py", "b.py"], "summary": "done"}, T2, "someone") == 0
    assert row(store_conn) == before


def test_a_different_value_does_replace_the_stored_one(store_conn):
    seed(store_conn)
    record_merge(store_conn, "i1", "t1", "abc123", T1, "merger")
    assert record_merge(store_conn, "i1", "t1", "def456", T2, "merger") == 1
    assert row(store_conn)["merged_commit"] == "def456"


def test_the_writers_touch_only_the_named_task(store_conn):
    seed(store_conn, "t1")
    seed(store_conn, "t2")
    record_merge(store_conn, "i1", "t1", "abc123", T2, "merger")
    record_handoff(store_conn, "i1", "t1", HANDOFF, T2, "builder")
    other = row(store_conn, "t2")
    assert (other["merged"], other["handoff"], other["updated_at"]) == (False, None, T1)
