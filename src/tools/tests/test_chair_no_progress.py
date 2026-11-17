"""H4.1: an initiative whose last runs ended the same way, with no new task state, is held for a person, once."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair_exec, chair_facts, route_hold, run_store
from agent_tools.chair_no_progress import (
    DEFAULT_NO_PROGRESS_AFTER,
    hold_reason,
    no_progress_facts,
    no_progress_key,
    plan_no_progress,
    repeated,
    resolve_no_progress_after,
)
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_read_run_outcomes import newest_runs, outcomes_from_rows, read_run_outcomes
from harness.store_migrate import open_store
from harness.store_write import Store

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
_STUCK = "phase p1 is not merged into init/demo: approved task(s) demo-t1 are not merged into epic/demo/p1"


def _outcome(run, p1="partial", p2="blocked", tasks=None, reason=_STUCK):
    return {"run_id": run, "phases": {"p1": p1, "p2": p2}, "reasons": {"p1": reason, "p2": "parent phase 'p1' is not merged"},
            "tasks": tasks or {}}


@pytest.mark.parametrize("value, expected", [
    (None, DEFAULT_NO_PROGRESS_AFTER), (2, 2), (3, 3), (0, 0), (1, DEFAULT_NO_PROGRESS_AFTER), (-1, DEFAULT_NO_PROGRESS_AFTER),
    (2.5, DEFAULT_NO_PROGRESS_AFTER), (3.0, 3), (True, DEFAULT_NO_PROGRESS_AFTER), ("2", DEFAULT_NO_PROGRESS_AFTER),
])
def test_resolve_no_progress_after_takes_zero_or_a_whole_number_of_at_least_two(value, expected):
    assert resolve_no_progress_after(value) == expected


def test_two_runs_with_the_same_phase_states_and_no_task_repeat():
    runs = [_outcome("demo-55"), _outcome("demo-54", reason="worded otherwise"), _outcome("demo-53", p1="complete")]
    assert [o["run_id"] for o in repeated(runs, 2, {})] == ["demo-55", "demo-54"]


@pytest.mark.parametrize("older", [
    _outcome("demo-54", p1="complete"),
    _outcome("demo-54", tasks={"demo-t1": "approved"}),
], ids=["another-phase-state", "a-task-state"])
def test_a_run_that_ended_otherwise_is_progress(older):
    assert repeated([_outcome("demo-55"), older], 2, {}) is None


def test_fewer_counted_runs_than_the_setting_or_a_run_with_no_phase_record_hold_nothing():
    assert repeated([_outcome("demo-55")], 2, {}) is None
    assert repeated([_outcome("demo-55"), _outcome("demo-54")], 3, {}) is None
    assert repeated([_outcome("demo-55"), _outcome("demo-54")], 0, {}) is None
    empty = {"run_id": "demo-54", "phases": {}, "reasons": {}, "tasks": {}}
    assert repeated([{**empty, "run_id": "demo-55"}, empty], 2, {}) is None


def test_a_release_mark_leaves_the_runs_before_it_out_so_a_fixed_initiative_gets_fresh_runs():
    runs = [_outcome("demo-55"), _outcome("demo-54"), _outcome("demo-53")]
    assert repeated(runs, 2, {"demo": 54}) is None
    assert repeated([_outcome("demo-57"), _outcome("demo-56"), *runs], 2, {"demo": 54}) is not None


def test_the_hold_reason_is_marked_for_a_person_and_names_the_runs_and_the_repeated_outcome():
    reason = hold_reason("demo", [_outcome("demo-55"), _outcome("demo-54")])
    assert reason.startswith("needs you: no progress: runs demo-54, demo-55 ended the same way and changed no task: ")
    assert "p1 partial, p2 blocked; p1: " + _STUCK in reason
    assert reason.endswith("`cox route release demo`")
    assert "\n" not in reason


def _facts(**overrides):
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [{"id": "demo", "repo": "r", "started": True, "ready_tasks": [{"id": "demo-t2", "needs": []}], "landed": set()}],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {"demo": True},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": _NOW.isoformat(),
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}


_FOUND = no_progress_facts({"demo": [_outcome("demo-55"), _outcome("demo-54")]}, {}, 2)


def _for(actions, initiative="demo"):
    return [a for a in actions if a.get("initiative") == initiative and a["kind"] not in ("unlaunched",)]


def test_without_the_fact_the_initiative_relaunches_and_with_it_the_chair_holds_it_instead():
    assert [a["kind"] for a in _for(plan_tick(_facts(), _NOW))] == ["clear_branches", "relaunch"]
    held = [{k: v for k, v in a.items() if k != "epoch"} for a in _for(plan_tick(_facts(no_progress=_FOUND), _NOW))]
    assert held == [{"kind": "hold_initiative", "initiative": "demo", "cause": "no_progress",
                     "reason": _FOUND["demo"]["reason"], "key": no_progress_key("demo", "demo-55")}]


def test_the_hold_is_planned_once_and_not_for_an_initiative_already_held():
    assert plan_no_progress(_facts(no_progress=_FOUND)) != []
    assert plan_no_progress(_facts(no_progress=_FOUND, handled_keys=[no_progress_key("demo", "demo-55")])) == []
    assert plan_no_progress(_facts(no_progress=_FOUND, held_initiatives=["demo"])) == []


def test_newest_runs_reads_only_the_initiative_s_own_series_newest_first():
    ids = ["demo-9", "demo-10", "demo.p1-30", "other-50", "demo-x", "demo-11"]
    assert newest_runs(ids, ["demo"], 2) == {"demo": ["demo-11", "demo-10"]}


def test_outcomes_from_rows_reads_each_phase_status_and_reason_and_each_task_status():
    phase_rows = [
        {"run_id": "demo-12", "phase_id": "p1", "record_json": json.dumps({"status": "partial", "reason": _STUCK})},
        {"run_id": "demo-11", "phase_id": "p1", "record_json": {"status": "complete"}},
    ]
    task_rows = [{"run_id": "demo-11", "task_id": "demo-t1", "record_json": {"status": "approved"}},
                 {"run_id": "demo-11", "task_id": "demo-t2", "record_json": "not json"}]
    assert outcomes_from_rows({"demo": ["demo-12", "demo-11"]}, phase_rows, task_rows) == {"demo": [
        {"run_id": "demo-12", "phases": {"p1": "partial"}, "reasons": {"p1": _STUCK}, "tasks": {}},
        {"run_id": "demo-11", "phases": {"p1": "complete"}, "reasons": {"p1": ""}, "tasks": {"demo-t1": "approved", "demo-t2": ""}},
    ]}


def test_no_progress_from_reads_the_cutoff_off_the_initiative_row():
    rows = [{"kind": "initiative", "initiative": "demo", "task_id": "demo", "state": "todo", "extra": {"fresh_after_run": {"demo": 54}}}]
    outcomes = {"demo": [_outcome("demo-55"), _outcome("demo-54")]}
    assert chair_facts.no_progress_from(rows, outcomes, 2) == {}
    assert chair_facts.no_progress_from([], outcomes, 2) == _FOUND
    assert chair_facts.no_progress_from([], outcomes, 0) == {}


# --- end to end: the real run store, the real reader, planner, executor and release ---

def _row(kind, task_id, **extra_fields):
    return {"kind": kind, "initiative": "demo", "task_id": task_id, "phase": "" if kind == "initiative" else "p1",
            "state": "todo" if kind == "initiative" else "ready", "needs": [], "surfaces": [], "title": task_id, "body": "",
            "extra": dict(extra_fields)}


def _end_runs(runs_dir: Path, *numbers: int) -> None:
    """Each run as the epic driver records it: its runs row, its phase records, then how it ended."""
    conn = open_store(run_store._store_url(runs_dir), "2026-10-07T00:00:00Z")
    try:
        store = Store(conn)
        for n in numbers:
            run = f"demo-{n}"
            store.record_run({"run_id": run})
            store.record_phase({"run_id": f"{run}:p1", "ts": f"2026-10-07T{n % 24:02d}:00:00Z", "status": "partial", "reason": _STUCK})
            store.record_phase({"run_id": f"{run}:p2", "ts": f"2026-10-07T{n % 24:02d}:00:01Z", "status": "blocked",
                                "reason": "parent phase 'p1' is not merged"})
            store.finish_run(run, f"2026-10-07T{n % 24:02d}:00:02Z", "ok")
    finally:
        conn.close()


def _tick_facts(runs_dir: Path, handled=()):
    rows = run_store.read_queue(runs_dir)
    found = chair_facts.no_progress_from(rows, read_run_outcomes(runs_dir, ["demo"], 2), 2)
    held = sorted(r["task_id"] for r in rows if r["kind"] == "initiative" and r["extra"].get("held"))
    return _facts(no_progress=found, held_initiatives=held, handled_keys=list(handled))


def _deps(ws: Path, handled: list):
    """Every slot a hold_initiative never reaches is inert; the store, files and courier are the real ones."""
    runs_dir = ws / "runs"
    return chair_exec.Deps(
        run=lambda argv: (1, "not used"), delete_branches=lambda repo, pattern: ([], "not used"), acquire_lease=lambda *a, **k: "",
        record=lambda action: None, run_id=lambda a: "", repo_for=lambda a: "",
        initiative_rows=lambda: run_store.read_queue(runs_dir), runs_dir=runs_dir, work_dir=ws, mark_handled=handled.append,
    )


@pytest.mark.usefixtures("board_store")
def test_end_to_end_two_same_runs_hold_the_initiative_once_and_a_release_waits_for_two_fresh_ones(tmp_path):
    runs_dir, handled = tmp_path / "runs", []
    (tmp_path / "work" / "demo").mkdir(parents=True)
    (tmp_path / "work" / "demo" / "initiative.md").write_text("---\ntitle: Demo\nid: demo\n---\nBody.\n", encoding="utf-8")
    assert run_store.upsert_row(runs_dir, _row("initiative", "demo"))
    assert run_store.upsert_row(runs_dir, _row("task", "demo-t2"))
    _end_runs(runs_dir, 54, 55)

    actions = _for(plan_tick(_tick_facts(runs_dir), _NOW))
    assert [a["kind"] for a in actions] == ["hold_initiative"]
    result = chair_exec._execute(actions[0], _deps(tmp_path, handled), {})
    assert result["status"] == "done", result

    row = next(r for r in run_store.read_queue(runs_dir) if r["kind"] == "initiative")
    reason = row["extra"]["hold_reason"]
    assert row["extra"]["held"] == "true"
    assert reason.startswith("needs you: no progress: runs demo-54, demo-55 ended the same way")
    assert f"hold_reason: {json.dumps(reason)}" in (tmp_path / "work/demo/initiative.md").read_text(encoding="utf-8")
    sent = [json.loads(line) for line in (tmp_path / "courier.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(e["ref"], e["event"]) for e in sent] == [("coxswain://initiative/demo", "needs_attention")]
    assert handled == ["no_progress:demo:demo-55"]

    # Held: the next tick neither relaunches nor holds it again, and a second hold writes nothing.
    assert _for(plan_tick(_tick_facts(runs_dir, handled), _NOW)) == []
    assert chair_exec._execute(actions[0], _deps(tmp_path, handled), {})["status"] == "skipped"
    assert len((tmp_path / "courier.jsonl").read_text(encoding="utf-8").splitlines()) == 1

    # The operator's release marks demo-55; the runs up to it no longer count, so the initiative relaunches.
    assert route_hold.release(tmp_path, runs_dir, "demo", run_store.read_queue(runs_dir)) == 0
    assert [a["kind"] for a in _for(plan_tick(_tick_facts(runs_dir, handled), _NOW))] == ["clear_branches", "relaunch"]
    _end_runs(runs_dir, 56)
    assert [a["kind"] for a in _for(plan_tick(_tick_facts(runs_dir, handled), _NOW))] == ["clear_branches", "relaunch"]
    _end_runs(runs_dir, 57)
    assert [a["kind"] for a in _for(plan_tick(_tick_facts(runs_dir, handled), _NOW))] == ["hold_initiative"]
