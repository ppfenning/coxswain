"""A phase the lander merged into its initiative branch reads merged in the feed, though its tasks are still approved."""
import pytest

from agent_tools import dash_feed

INITIATIVE = "example-initiative"


def _task(phase, task_id, state, needs=()):
    return {"kind": "task", "initiative": INITIATIVE, "phase": phase, "task_id": task_id, "state": state, "needs": list(needs), "extra": {}}


ROWS = [
    _task("p1", "a", "approved"),
    _task("p1", "b", "approved"),
    _task("p2", "c", "approved", ["a"]),
    _task("p3", "d", "ready", ["c"]),
]
RECORDS = [{"initiative": INITIATIVE, "phases": {"p1": {"state": "merged"}, "p2": {"state": "open"}}}]


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: [dict(r, extra=dict(r["extra"])) for r in ROWS])
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: [])
    monkeypatch.setattr(dash_feed, "_landing_records", lambda runs_dir: RECORDS)


def _row(tmp_path):
    return dash_feed._queue(tmp_path, {})[0][0]


def test_a_merged_phase_reads_merged_and_its_tasks_read_merged(store, tmp_path):
    phases = {p["id"]: p for p in _row(tmp_path)["phases"]}
    assert [t["state"] for t in phases["p1"]["tasks"]] == ["merged", "merged"]
    assert [t["state"] for t in phases["p2"]["tasks"]] == ["merged"]  # merged into its own phase tip
    assert (phases["p1"]["status"], phases["p2"]["status"]) == ("merged", "approved")


def test_the_phase_counts_merged_work_as_done_and_names_the_next_one_current(store, tmp_path):
    row = _row(tmp_path)
    assert (row["phases_landed"], row["phases_total"], row["current_phase"]) == (1, 3, "p2")


def test_a_child_of_a_merged_phase_no_longer_waits_on_it(store, tmp_path):
    live = {(INITIATIVE, "p2"): "run-1"}
    row = dash_feed._queue(tmp_path, live)[0][0]
    status = {p["id"]: (p["status"], p["reason"]) for p in row["phases"]}
    assert status["p1"] == ("merged", None)
    assert status["p2"] == ("running", None)
    assert status["p3"][0] == "waiting" and "p1" not in (status["p3"][1] or "")


def test_an_initiative_with_every_phase_merged_stays_in_the_queue_until_it_lands(store, tmp_path, monkeypatch):
    all_merged = [{"initiative": INITIATIVE, "phases": {p: {"state": "merged"} for p in ("p1", "p2", "p3")}}]
    monkeypatch.setattr(dash_feed, "_landing_records", lambda runs_dir: all_merged)
    approved = [dict(r, state="approved", extra={}) for r in ROWS]
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: approved)
    rows, total = dash_feed._queue(tmp_path, {})
    assert total == 1 and rows[0]["phases_landed"] == rows[0]["phases_total"] == 3


def test_no_landing_record_changes_nothing(store, tmp_path, monkeypatch):
    monkeypatch.setattr(dash_feed, "_landing_records", lambda runs_dir: [])
    row = _row(tmp_path)
    assert row["phases_landed"] == 0 and all(t["state"] != "merged" for p in row["phases"] for t in p["tasks"])


# Task level: under the lander, an approved task is merged into its phase tip, so it is complete at its own level.
PHASE_ROWS = [
    _task("p1", "a", "approved"),
    _task("p1", "b", "ready", ["a"]),
    _task("p1", "c", "ready", ["x"]),
    _task("p1", "x", "ready"),
]
OPEN_RECORD = [{"initiative": INITIATIVE, "phases": {"p1": {"state": "open"}}}]


@pytest.fixture
def phase_store(monkeypatch):
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: [dict(r, extra={}) for r in PHASE_ROWS])
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: [])
    monkeypatch.setattr(dash_feed, "_landing_records", lambda runs_dir: OPEN_RECORD)


def _states(tmp_path):
    return {t["id"]: t["state"] for p in _row(tmp_path)["phases"] for t in p["tasks"]}


def test_an_approved_task_under_the_lander_reads_merged(phase_store, tmp_path):
    assert _states(tmp_path)["a"] == "merged"


def test_a_task_whose_sibling_merged_into_the_phase_tip_is_ready(phase_store, tmp_path):
    assert _states(tmp_path)["b"] == "ready"


def test_a_task_whose_sibling_has_not_merged_is_still_held_behind_it(phase_store, tmp_path):
    # `blocked` until the feed and towpath share a `waiting` word for a task.
    assert _states(tmp_path)["c"] == "blocked"


def test_a_phase_whose_tasks_all_merged_into_its_tip_waits_for_the_lander(monkeypatch, tmp_path):
    rows = [dict(_task("p1", "a", "approved"), extra={}), dict(_task("p1", "b", "approved", ["a"]), extra={})]
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: rows)
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: [])
    monkeypatch.setattr(dash_feed, "_landing_records", lambda runs_dir: OPEN_RECORD)
    (phase,) = dash_feed._queue(tmp_path, {})[0][0]["phases"]
    assert (phase["status"], phase["reason"]) == ("approved", "built; waiting for the lander to merge it into the initiative")


def test_without_a_landing_record_an_approved_task_still_reads_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: [dict(r, extra={}) for r in PHASE_ROWS])
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: [])
    monkeypatch.setattr(dash_feed, "_landing_records", lambda runs_dir: [])
    assert _states(tmp_path)["a"] == "approved"
