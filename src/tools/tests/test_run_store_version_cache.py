import sqlite3

import pytest

from agent_tools import run_store
from agent_tools.chair_read_cache import StampCache
from harness.store_api import Failed, NotAvailable, TableStamp


def _row(initiative="i1", task="t1", state="open", kind="task", needs=("a",)):
    return {"kind": kind, "initiative": initiative, "task_id": task, "phase": "p1", "state": state, "needs": list(needs),
            "surfaces": [], "title": "T", "body": ""}


@pytest.fixture
def runs(tmp_path):
    """A runs directory whose real sqlite store holds one row, i1/t1."""
    assert run_store.upsert_row(tmp_path, _row())
    return tmp_path


READERS = {
    "work_items": lambda runs, cache, **kw: run_store.work_items(runs, cache=cache, **kw),
    "queue": lambda runs, cache, **kw: run_store.read_queue(runs, cache=cache, **kw),
}


@pytest.fixture(params=sorted(READERS))
def read(request, runs):
    return lambda cache, **kw: READERS[request.param](runs, cache, **kw)


def test_an_unchanged_stamp_skips_the_full_read(read):
    cache = StampCache(4)
    first, second = read(cache), read(cache)
    assert cache.reads == (1, 1)
    assert first == second
    assert [r["task_id"] for r in first] == ["t1"]


def test_a_changed_row_reads_again_and_returns_the_new_rows(runs, read):
    cache = StampCache(4)
    assert [r["state"] for r in read(cache)] == ["open"]
    assert run_store.upsert_row(runs, _row(state="done"))
    assert [r["state"] for r in read(cache)] == ["done"]
    assert cache.reads == (0, 2)


def test_a_changed_count_reads_again(runs, read):
    cache = StampCache(4)
    read(cache)
    assert run_store.upsert_row(runs, _row(task="t2"))
    assert [r["task_id"] for r in read(cache)] == ["t1", "t2"]
    assert cache.reads == (0, 2)


def test_an_unopenable_store_reads_empty_and_stores_nothing(store_at, read):
    store_at("nosuch://x")
    cache = StampCache(4)
    assert read(cache) == []
    assert read(cache) == []
    assert (cache.reads, len(cache)) == ((0, 2), 0)


def test_a_caller_that_mutates_a_row_cannot_change_the_stored_rows(read):
    cache = StampCache(4)
    first = read(cache)
    first[0]["state"] = "poisoned"
    first.append({"extra": 1})
    again = read(cache)
    assert cache.reads == (1, 1)
    assert [r["state"] for r in again] == ["open"]


def test_the_work_items_key_includes_the_initiative(runs):
    assert run_store.upsert_row(runs, _row(initiative="i2"))
    cache = StampCache(4)
    run_store.work_items(runs, "i1", cache=cache)
    run_store.work_items(runs, "i2", cache=cache)
    run_store.work_items(runs, "i1", cache=cache)
    assert cache.reads == (1, 2)
    assert len(cache) == 2


def test_the_queue_key_includes_the_initiative_and_the_kind(runs):
    cache = StampCache(8)
    for initiative, kind in [(None, None), ("i1", None), (None, "task"), ("i1", "task"), ("i1", "task")]:
        run_store.read_queue(runs, initiative, kind, cache=cache)
    assert cache.reads == (1, 4)
    assert len(cache) == 4


def test_the_two_readers_do_not_share_an_entry(runs):
    cache = StampCache(4)
    run_store.work_items(runs, cache=cache)
    run_store.read_queue(runs, cache=cache)
    assert cache.reads == (0, 2)


def test_a_filter_that_looks_like_sql_is_a_value_not_a_statement(runs):
    cache = StampCache(4)
    assert run_store.read_queue(runs, "i'; DROP TABLE work_items; --", "task", cache=cache) == []
    assert [r["task_id"] for r in run_store.read_queue(runs)] == ["t1"]


@pytest.mark.parametrize(
    ("found", "expected_count"),
    [(TableStamp(3, "2026-11-06", "d"), 3), (TableStamp(0, None, "d"), 0), (Failed(2, "x"), None), (NotAvailable("x"), None)],
)
def test_the_stamp_of_a_probe_is_count_led_and_none_when_the_store_cannot_say(found, expected_count):
    stamp = run_store._stamp(found)
    assert (None if stamp is None else stamp[0]) == expected_count


def test_the_stamp_differs_when_only_the_newest_or_the_digest_differs():
    assert run_store._stamp(TableStamp(1, "a", "d")) != run_store._stamp(TableStamp(1, "b", "d"))
    assert run_store._stamp(TableStamp(1, "a", "d")) != run_store._stamp(TableStamp(1, "a", "e"))
    assert run_store._stamp(TableStamp(1, "a", "d")) == run_store._stamp(TableStamp(1, "a", "d"))


def test_the_queue_stamp_is_stable_while_nothing_changes(runs):
    assert run_store.queue_stamp(runs) == run_store.queue_stamp(runs)


def test_a_state_change_on_a_non_newest_row_at_the_same_updated_at_changes_the_stamp(runs):
    assert run_store.upsert_row(runs, _row(task="t2"))
    before = run_store.queue_stamp(runs)
    with sqlite3.connect(runs / "cox.db") as conn:
        conn.execute("UPDATE work_items SET updated_at = '2026-11-06T01' WHERE 1 = 1")
    middle = run_store.queue_stamp(runs)
    with sqlite3.connect(runs / "cox.db") as conn:
        conn.execute("UPDATE work_items SET state = 'done' WHERE task_id = 't1'")
    after = run_store.queue_stamp(runs)
    assert before != middle != after
    assert middle[0] == after[0] == 2


def test_a_claim_or_release_changes_the_queue_stamp(runs):
    before = run_store.queue_stamp(runs)
    assert run_store.claim_outcome(runs, "i1", "t1", "me", 300, "2026-11-06T00:00:00Z")[0]
    claimed = run_store.queue_stamp(runs)
    assert run_store.release_row(runs, "i1", "t1", "me")
    released = run_store.queue_stamp(runs)
    assert len({before, claimed, released}) == 3


def test_a_good_probe_followed_by_a_failed_read_is_not_cached_under_the_good_stamp():
    # The probe said (2, 7); the full read then failed. That failure reads as [] and is never stored, so the next read
    # under the same stamp reads again instead of serving an empty table until a write moves the stamp.
    cache, stamp, key = StampCache(4), (2, 7), ("queue", "/runs", None, None)
    reads = iter([None, [{"task_id": "t1"}]])
    assert run_store._versioned(cache, key, stamp, lambda: next(reads)) == []
    assert run_store._versioned(cache, key, stamp, lambda: next(reads)) == [{"task_id": "t1"}]
    assert run_store._versioned(cache, key, stamp, lambda: [{"task_id": "never read"}]) == [{"task_id": "t1"}]
