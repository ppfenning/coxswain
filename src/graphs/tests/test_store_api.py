"""harness.store_api: the pure pieces with example inputs, and each url-level call against a real sqlite store.

No doubles: the sqlite file is the suites' stand-in for Postgres (`COXSWAIN_STORE_DOUBLE`), opened and migrated by the
same `open_store` production uses. Postgres itself is covered by the smoke suite, which drives `cox` against one.
"""

from typing import get_args

import pytest

from core.workstore import STATES
from harness import store_api, store_cli
from harness.store_api import (
    Claimed,
    Failed,
    Held,
    Landed,
    LeaseGranted,
    LeaseRefused,
    LeaseReleased,
    MergeRecorded,
    NotAvailable,
    NotInStore,
    Paused,
    PauseRefused,
    Released,
    Resolved,
    Resumed,
    ResumeRefused,
    StateRefused,
    StateSet,
    TableStamp,
    Upserted,
)
from harness.store_dialect import POSTGRES, SQLITE
from harness.store_migrate import PG_DUMP_TIMEOUT_S, exempt_from_bounds, open_store
from harness.store_work_state import set_state
from harness.store_write import Store

NOW = "2026-11-17T12:00:00Z"
LATER = "2026-11-17T12:05:00Z"


def _url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'cox.db'}"


def _choices(parser, *path: str) -> dict[str, tuple[str, ...]]:
    """Pure over a parser: each positional and option of the subcommand at `path`, by dest, to its choices."""
    for name in path:
        sub = next(a for a in parser._actions if a.dest in ("command", "action"))
        parser = sub.choices[name]
    return {a.dest: tuple(a.choices) for a in parser._actions if a.choices is not None}


# --- the enums ------------------------------------------------------------------------------------------------------


def test_the_api_work_states_are_the_work_store_states():
    assert store_api.WORK_STATES == STATES == get_args(store_api.WorkState)


def test_the_store_cli_set_state_choices_equal_the_api_enums():
    choices = _choices(store_cli.build_parser(), "set-state")
    assert choices == {"state": store_api.WORK_STATES, "expect": store_api.WORK_STATES}


# --- pure -----------------------------------------------------------------------------------------------------------


def test_a_refusal_names_the_state_found_or_no_row():
    assert store_api.refusal_detail("t1", "done", "ready") == "work item t1 is in state done, expected ready"
    assert store_api.refusal_detail("t1", None, "ready") == "work item t1 is in state no row, expected ready"


def test_a_stamp_counts_rows_takes_the_newest_time_and_changes_with_any_column():
    rows = [("2026-01-02", "a", 1), ("2026-01-01", "b", 2), (None, "c", 3)]
    stamp = store_api.stamp_of(rows, 0)
    assert (stamp.count, stamp.newest) == (3, "2026-01-02")
    assert store_api.stamp_of(rows, 0) == stamp
    assert store_api.stamp_of([*rows[:2], (None, "c", 4)], 0) != stamp


def test_an_empty_slice_has_no_newest_time():
    assert store_api.stamp_of([], 0).count == 0 and store_api.stamp_of([], 0).newest is None


def test_a_done_phase_drops_its_landed_mark_and_names_the_branch():
    record = {"phase": "p1", "manifest_record": {"status": "landed", "landed": {"pr": "x"}, "tasks": ["t1"]}}
    assert store_api.phase_done_record(record, "phase/p1") == {
        "phase": "p1",
        "manifest_record": {"status": "done", "merged_into": "phase/p1", "tasks": ["t1"]},
    }


def test_a_phase_with_no_manifest_gets_one():
    assert store_api.phase_done_record({}, "b") == {"manifest_record": {"status": "done", "merged_into": "b"}}


def test_the_lease_payloads_keep_the_cli_contract():
    assert store_cli.lease_payload("acquire", LeaseGranted(3, "a")) == {"ok": True, "epoch": 3, "holder": "a"}
    assert store_cli.lease_payload("renew", LeaseGranted(3, "a")) == {"ok": True, "epoch": None, "holder": None}
    assert store_cli.lease_payload("release", LeaseReleased()) == {"ok": True, "epoch": None, "holder": None}
    assert store_cli.lease_payload("acquire", LeaseRefused(2, "b")) == {"ok": False, "epoch": 2, "holder": "b"}


# --- against a real store -------------------------------------------------------------------------------------------


def test_a_store_that_cannot_open_is_not_available_and_never_raises():
    assert isinstance(store_api.set_work_state("nosuch://x", "i", "t", "ready", "me", NOW), NotAvailable)
    assert isinstance(store_api.work_item_rows("nosuch://x"), NotAvailable)


def test_a_read_never_creates_a_store(tmp_path):
    assert isinstance(store_api.queue_rows(_url(tmp_path)), NotAvailable)
    assert not (tmp_path / "cox.db").exists()


def test_set_state_writes_a_new_row_only_with_a_phase(tmp_path):
    url = _url(tmp_path)
    assert store_api.set_work_state(url, "i", "t1", "ready", "me", NOW) == StateRefused(
        "no work item t1 in initiative i and --phase was not given"
    )
    written = store_api.set_work_state(url, "i", "t1", "ready", "me", NOW, phase="p1")
    assert written == StateSet(
        {"initiative": "i", "task_id": "t1", "phase": "p1", "state": "ready", "needs": [], "updated_at": NOW, "updated_by": "me"}
    )
    rows = store_api.work_item_rows(url, "i")
    assert [(r["task_id"], r["phase"], r["state"]) for r in rows] == [("t1", "p1", "ready")]


def test_a_compare_and_set_applies_only_on_the_state_it_names_and_reports_the_one_it_found(tmp_path):
    url = _url(tmp_path)
    store_api.set_work_state(url, "i", "t1", "ready", "me", NOW, phase="p1")
    assert store_api.set_work_state(url, "i", "t1", "done", "me", NOW, expected="approved") == StateRefused(
        "work item t1 is in state ready, expected approved", "ready"
    )
    assert isinstance(store_api.set_work_state(url, "i", "t1", "approved", "me", LATER, expected="ready"), StateSet)
    assert store_api.set_work_state(url, "i", "t2", "done", "me", NOW, expected="approved").current is None


def test_a_merge_is_recorded_on_an_approved_row_and_refused_on_any_other(tmp_path):
    url = _url(tmp_path)
    store_api.set_work_state(url, "i", "t1", "ready", "me", NOW, phase="p1")
    refused = store_api.record_task_merge(url, "i", "t1", "abc", "me", NOW)
    assert isinstance(refused, StateRefused) and "not 'approved'" in refused.detail
    store_api.set_work_state(url, "i", "t1", "approved", "me", NOW)
    assert store_api.record_task_merge(url, "i", "t1", "abc", "me", NOW) == MergeRecorded(1)
    assert store_api.record_task_merge(url, "i", "t1", "abc", "me", NOW) == MergeRecorded(0)


def _seed_run(url: str) -> None:
    conn = open_store(url, NOW)
    try:
        store = Store(conn)
        store.record_run({"run_id": "r1", "graph": "g", "status": "running", "started_at": NOW})
        store.record_phase({"run_id": "r1:p1", "status": "landed", "manifest_record": {"status": "landed"}})
        store.record_task_record("r1", "p1", "t1", {"task": "t1", "status": "approved"}, NOW)
    finally:
        conn.close()


def test_a_task_record_is_read_marked_landed_and_listed(tmp_path):
    url = _url(tmp_path)
    _seed_run(url)
    assert store_api.task_record(url, "r1", "p1", "t1") == {"task": "t1", "status": "approved"}
    assert store_api.task_record_phases(url, "r1", "t1") == ["p1"]
    assert store_api.run_task_ids(url, "r1") == ["t1"]
    landed = store_api.mark_task_landed(url, "r1", "p1", "t1", "https://x/pull/1", LATER, NOW)
    assert landed == Landed({"task": "t1", "status": "approved", "landed": {"pr": "https://x/pull/1", "at": LATER}})
    assert store_api.mark_task_landed(url, "r1", "p1", "t9", "https://x/pull/1", LATER, NOW) == NotInStore()


def test_a_phase_is_marked_done_and_a_missing_one_refused(tmp_path):
    url = _url(tmp_path)
    _seed_run(url)
    assert store_api.record_phase_done(url, "r1", "p1", "phase/p1", NOW) == MergeRecorded(1)
    assert isinstance(store_api.record_phase_done(url, "r1", "p9", "phase/p9", NOW), StateRefused)


def test_a_run_pauses_and_resumes_and_an_unknown_one_is_refused(tmp_path):
    url = _url(tmp_path)
    _seed_run(url)
    assert store_api.pause_run(url, "r1", NOW, "lunch") == Paused("lunch")
    assert store_api.resume_run(url, "r1", NOW) == Resumed()
    assert store_api.pause_run(url, "r9", NOW) == PauseRefused()
    assert store_api.resume_run(url, "r9", NOW) == ResumeRefused()


def test_a_lease_is_granted_renewed_refused_to_another_and_released(tmp_path):
    url = _url(tmp_path)
    granted = store_api.acquire_lease(url, "chair", "a", 600, NOW)
    assert isinstance(granted, LeaseGranted) and granted.holder == "a"
    assert store_api.acquire_lease(url, "chair", "b", 300, NOW) == LeaseRefused(granted.epoch, "a")
    assert store_api.renew_lease(url, "chair", "a", granted.epoch, 300, LATER) == LeaseGranted(granted.epoch, "a")
    assert store_api.renew_lease(url, "chair", "b", granted.epoch, 300, LATER) == LeaseRefused(None, None)
    assert store_api.release_lease(url, "chair", "a", granted.epoch, LATER) == LeaseReleased()


def test_a_queue_row_is_upserted_claimed_held_and_released(tmp_path):
    url = _url(tmp_path)
    row = {"initiative": "i", "task_id": "t1", "kind": "task", "phase": "p1", "state": "ready", "needs": [], "title": "T"}
    assert store_api.upsert_queue_row(url, row, NOW) == Upserted(True)
    assert store_api.upsert_queue_row(url, row, NOW) == Upserted(False)
    assert [(r["task_id"], r["kind"], r["title"]) for r in store_api.queue_rows(url, "i", "task")] == [("t1", "task", "T")]
    claimed = store_api.claim_queue_row(url, "i", "t1", "a", 60, NOW)
    assert claimed == Claimed({"holder": "a", "epoch": 1, "expires_at": "2026-11-17T12:01:00Z"})
    assert store_api.claim_queue_row(url, "i", "t1", "b", 60, NOW) == Held()
    assert store_api.release_queue_row(url, "i", "t1", "b", NOW) == Released(False)
    assert store_api.release_queue_row(url, "i", "t1", "a", NOW) == Released(True)


def test_a_bad_priority_is_failed_with_its_reason(tmp_path):
    row = {"initiative": "i", "task_id": "t1", "kind": "task", "phase": "p1", "state": "ready", "needs": [], "priority": "high"}
    found = store_api.upsert_queue_row(_url(tmp_path), row, NOW)
    assert isinstance(found, Failed) and "priority" in found.detail


def test_the_stamp_moves_when_a_claim_changes_a_row_without_touching_updated_at(tmp_path):
    url = _url(tmp_path)
    store_api.upsert_queue_row(url, {"initiative": "i", "task_id": "t1", "kind": "task", "phase": "p1", "state": "ready", "needs": []}, NOW)
    before = store_api.work_items_stamp(url, [("initiative", "i")])
    store_api.claim_queue_row(url, "i", "t1", "a", 60, NOW)
    after = store_api.work_items_stamp(url, [("initiative", "i")])
    assert isinstance(before, TableStamp) and before.count == after.count == 1 and before != after


def test_export_hashes_round_trip(tmp_path):
    url = _url(tmp_path)
    assert store_api.export_hashes(url, NOW) == {}
    store_api.set_export_hash(url, "i", "t1", "aa", NOW)
    store_api.set_export_hash(url, "i", "t1", "bb", NOW)
    assert store_api.export_hashes(url, NOW) == {("i", "t1"): "bb"}


def test_a_short_id_resolves_to_its_run_and_initiatives_list_theirs(tmp_path):
    url = _url(tmp_path)
    _seed_run(url)
    store_api.upsert_queue_row(url, {"initiative": "ship", "task_id": "intake", "kind": "intake", "phase": "", "state": "done", "needs": []}, NOW)
    conn = open_store(url, NOW)
    try:
        conn.execute("UPDATE runs SET short_id = 'I4-1' WHERE run_id = 'r1'")
        conn.execute("UPDATE work_items SET short_id = 'I4' WHERE task_id = 'intake'")
    finally:
        conn.close()
    assert store_api.resolve_token(url, "I4-1") == Resolved("run", "r1")
    assert store_api.resolve_token(url, "r1") == Resolved("run", "r1")
    assert store_api.resolve_token(url, "I9-1") is None
    assert store_api.initiative_short_ids(url) == {"ship": "I4"}


def test_a_state_write_that_names_no_one_is_refused_and_opens_no_store(tmp_path):
    assert store_api.set_work_state(_url(tmp_path), "i", "t1", "ready", " ", NOW, phase="p1") == Failed(
        2, "who made the change (by) must not be empty"
    )
    assert not (tmp_path / "cox.db").exists()


def test_a_state_that_is_not_a_work_state_is_named():
    assert store_api.state_problem("ready", None) is None
    assert store_api.state_problem("ready", "done") is None
    assert store_api.state_problem("finished", None).startswith("unknown work state 'finished'; expected one of todo,")
    assert store_api.state_problem("ready", "Ready").startswith("unknown work state 'Ready'")


def test_an_unknown_state_or_expect_is_failed_and_opens_no_store(tmp_path):
    for state, expected in (("finished", None), ("ready", "nope")):
        found = store_api.set_work_state(_url(tmp_path), "i", "t1", state, "me", NOW, phase="p1", expected=expected)
        assert isinstance(found, Failed) and "unknown work state" in found.detail
    assert not (tmp_path / "cox.db").exists()


def test_the_low_level_setter_refuses_an_unknown_state_and_writes_nothing(tmp_path):
    url = _url(tmp_path)
    conn = open_store(url, NOW)
    try:
        with pytest.raises(ValueError, match="unknown work state 'finished'"):
            set_state(conn, "i", "t1", "finished", "me", "p1", NOW)
    finally:
        conn.close()
    assert store_api.work_item_rows(url, "i") == []


def test_a_row_that_will_not_encode_is_failed_not_raised(tmp_path):
    extra = {"x": object()}
    row = {"initiative": "i", "task_id": "t1", "kind": "task", "phase": "p1", "state": "ready", "needs": [], "extra": extra}
    found = store_api.upsert_queue_row(_url(tmp_path), row, NOW)
    assert isinstance(found, Failed) and found.detail.startswith("the store call failed: TypeError")


def test_a_migration_lifts_the_statement_and_lock_bounds_for_its_own_transaction_only():
    assert exempt_from_bounds(POSTGRES) == ("SET LOCAL statement_timeout = 0", "SET LOCAL lock_timeout = 0")
    assert exempt_from_bounds(SQLITE) == ()
    assert PG_DUMP_TIMEOUT_S > 0
