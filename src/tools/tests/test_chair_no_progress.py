"""H4.1: an initiative whose last runs ended the same way, with no new task state, is held for a person, once."""

import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools import chair_exec, chair_facts, cli, route_guard, route_hold, run_store, store_cli
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
from agent_tools.chair_read_run_outcomes import newest_runs, outcomes_from_rows
from harness import epic, run_lease, store_lease
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


def test_a_hold_a_person_cleared_without_a_release_lets_the_initiative_relaunch():
    """The hold for these runs was made (its key is handled) and is no longer visible: the chair relaunches it."""
    cleared = _facts(no_progress=_FOUND, handled_keys=[no_progress_key("demo", "demo-55")])
    assert [a["kind"] for a in _for(plan_tick(cleared, _NOW))] == ["clear_branches", "relaunch"]


def test_newest_runs_reads_the_initiative_s_own_series_newest_first_and_waits_for_a_live_run():
    runs = [("demo-9", True), ("demo-10", True), ("demo.p1-30", True), ("other-50", True), ("demo-x", True), ("demo-11", True)]
    assert newest_runs(runs, ["demo"], 2) == {"demo": ["demo-11", "demo-10"]}
    assert newest_runs([*runs, ("demo-12", False)], ["demo"], 2) == {}
    assert newest_runs([("demo-3", False), ("demo-2", True)], ["demo"], 2) == {}


def test_outcomes_from_rows_reads_each_phase_status_and_reason_and_each_task_state():
    phase_rows = [
        {"run_id": "demo-12", "phase_id": "p1", "record_json": json.dumps({"status": "partial", "reason": _STUCK})},
        {"run_id": "demo-11", "phase_id": "p1", "record_json": {"status": "complete"}},
        {"run_id": "demo-11", "phase_id": "p2", "record_json": "not json"},
    ]
    task_rows = [{"run_id": "demo-11", "task_id": "demo-t1", "state": "approved"}]
    assert outcomes_from_rows({"demo": ["demo-12", "demo-11"]}, phase_rows, task_rows) == {"demo": [
        {"run_id": "demo-12", "phases": {"p1": "partial"}, "reasons": {"p1": _STUCK}, "tasks": {}},
        {"run_id": "demo-11", "phases": {"p1": "complete", "p2": ""}, "reasons": {"p1": "", "p2": ""}, "tasks": {"demo-t1": "approved"}},
    ]}


def test_no_progress_from_reads_the_cutoff_off_the_initiative_row():
    rows = [{"kind": "initiative", "initiative": "demo", "task_id": "demo", "state": "todo", "extra": {"fresh_after_run": {"demo": 54}}}]
    outcomes = {"demo": [_outcome("demo-55"), _outcome("demo-54")]}
    assert chair_facts.no_progress_from(rows, outcomes, 2) == {}
    assert chair_facts.no_progress_from([], outcomes, 2) == _FOUND
    assert chair_facts.no_progress_from([], outcomes, 0) == {}


# --- end to end: runs written by the epic's own writers, read by the chair's real wiring ---

@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace with a throwaway store. The routing profile names a harness directory whose python is this
    interpreter, so every store command the chair's wiring runs, runs for real against it."""
    python = tmp_path / "harness" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\nharness_dir: {tmp_path / 'harness'}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    # conftest points every test's store_cli at no harness; this one owns a throwaway store, so the real resolver comes back.
    monkeypatch.setattr(store_cli, "_harness_python", run_store._harness_python)
    (tmp_path / "runs").mkdir()
    open_store(run_store._store_url(tmp_path / "runs"), "2026-01-01T00:00:00Z").close()
    (tmp_path / "work" / "demo").mkdir(parents=True)
    (tmp_path / "work" / "demo" / "initiative.md").write_text("---\ntitle: Demo\nid: demo\n---\nBody.\n", encoding="utf-8")
    for row in (_row("initiative", "demo", state="todo"), _row("task", "demo-t1", state="approved"), _row("task", "demo-t3", state="ready")):
        assert run_store.upsert_row(tmp_path / "runs", row)
    return tmp_path


def _row(kind, task_id, state):
    return {"kind": kind, "initiative": "demo", "task_id": task_id, "phase": "" if kind == "initiative" else "p1",
            "state": state, "needs": [], "surfaces": [], "title": task_id, "body": "", "extra": {}}


def _epic_run(ws: Path, n: int, *, tasks=(), ended=True) -> None:
    """One run as the epic records it: the launch's runs row, then `_record_phase` and `_record_tasks` for each phase,
    then `_record_run_exit`. Phase p1 ends partial and p2 blocked, every time."""
    conn = open_store(run_store._store_url(ws / "runs"), "2026-01-01T00:00:00Z")
    try:
        store, run = Store(conn), f"demo-{n}"
        store.record_run({"run_id": run})
        ctx = SimpleNamespace(store=store, retry=epic._StoreRetry(), run_id=run, epoch=None, lease_name=None)
        epic._record_phase(ctx, {"phase": "p1", "status": "partial", "reason": _STUCK})
        epic._record_tasks(ctx, "p1", [{"id": task, "status": status} for task, status in tasks], [])
        epic._record_phase(ctx, {"phase": "p2", "status": "blocked", "reason": "parent phase 'p1' is not merged"})
        if ended:
            epic._record_run_exit(store, run, "ok", epic._StoreRetry())
    finally:
        conn.close()


def _gathered(ws: Path, handled=()) -> dict:
    """The facts a real tick gathers: `gather_facts` over the chair's own `cli._chair_run_deps` for this workspace."""
    profile = {"workspace_dir": str(ws), "harness_dir": str(ws / "harness")}
    deps = cli._chair_run_deps(ws / "runs", profile, "chair", 1, "h", True, lambda line: None, ws / "profile.yaml", "store")
    facts = chair_facts.gather_facts(deps.facts_deps, _NOW)
    return {**facts, "handled_keys": [*facts.get("handled_keys", []), *handled]}


def _exec(ws: Path, action: dict, handled: list) -> dict:
    """The chair's own edge bundle, with the handled-key sink the tick binds."""
    deps = chair_exec.edge_deps(ws / "runs", ws, "chair", 1, run_id=lambda a: "", repo_for=lambda a: "",
                                record=lambda a: None, host="h", harness_python=sys.executable)
    return chair_exec._execute(action, dataclasses.replace(deps, mark_handled=handled.append), {})


def _held(ws: Path) -> dict:
    return next(r for r in run_store.read_queue(ws / "runs") if r["kind"] == "initiative")["extra"]


def test_end_to_end_two_same_runs_hold_the_initiative_once_and_a_release_waits_for_two_fresh_ones(ws):
    _epic_run(ws, 54)
    _epic_run(ws, 55)
    handled: list[str] = []
    # The first tick takes the chair's lease through the real executor, as a fresh chair does.
    assert plan_tick(_gathered(ws), _NOW) == [{"kind": "take_lease", "epoch": 0}]
    assert _exec(ws, {"kind": "take_lease", "epoch": 0}, handled)["status"] == "done"
    facts = _gathered(ws)
    assert facts["no_progress"]["demo"]["runs"] == ["demo-55", "demo-54"]
    planned = plan_tick(facts, _NOW)
    holds = [a for a in planned if a["kind"] == "hold_initiative"]
    assert [a["initiative"] for a in holds] == ["demo"]
    assert not [a for a in planned if a["kind"] in ("relaunch", "launch_epic") and a.get("initiative") == "demo"]

    assert _exec(ws, holds[0], handled)["status"] == "done"
    assert _held(ws)["held"] == "true"
    assert _held(ws)["hold_reason"].startswith("needs you: no progress: runs demo-54, demo-55 ended the same way")
    assert len((ws / "courier.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert handled == ["no_progress:demo:demo-55"]
    assert _exec(ws, holds[0], handled)["status"] == "skipped"
    assert len((ws / "courier.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert "demo" in _gathered(ws)["held_initiatives"]

    assert route_hold.release(ws, ws / "runs", "demo", run_store.read_queue(ws / "runs"), initiative_live=()) == 0
    assert _gathered(ws, handled)["no_progress"] == {}
    _epic_run(ws, 56)
    assert _gathered(ws, handled)["no_progress"] == {}
    _epic_run(ws, 57)
    assert _gathered(ws, handled)["no_progress"]["demo"]["runs"] == ["demo-57", "demo-56"]


def test_end_to_end_a_task_that_moved_on_is_progress_though_every_phase_state_repeats(ws):
    """The reviewer's case: p1 partial both runs, and demo-t3 went from revise to approved."""
    _epic_run(ws, 54, tasks=[("demo-t3", "revise")])
    _epic_run(ws, 55, tasks=[("demo-t3", "approved")])
    assert _gathered(ws)["no_progress"] == {}
    _epic_run(ws, 56, tasks=[("demo-t3", "approved")])
    assert _gathered(ws)["no_progress"]["demo"]["runs"] == ["demo-56", "demo-55"]


def test_end_to_end_a_newer_run_that_has_not_ended_holds_nothing(ws):
    _epic_run(ws, 54)
    _epic_run(ws, 55)
    _epic_run(ws, 56, ended=False)
    assert _gathered(ws)["no_progress"] == {}


def _held_by_the_chair(ws: Path) -> None:
    """Runs demo-54 and demo-55 ended the same way, and the chair's own tick and executor held demo."""
    _epic_run(ws, 54)
    _epic_run(ws, 55)
    assert _exec(ws, {"kind": "take_lease", "epoch": 0}, [])["status"] == "done"
    (hold,) = [a for a in plan_tick(_gathered(ws), _NOW) if a["kind"] == "hold_initiative"]
    assert _exec(ws, hold, [])["status"] == "done"
    assert _held(ws)["held"] == "true"


def _live_run(ws: Path, run: str) -> None:
    """A run in flight, as the epic holds one: its runs row and its `runs:` lease, live for an hour from now."""
    conn = open_store(run_store._store_url(ws / "runs"), "2026-01-01T00:00:00Z")
    try:
        Store(conn).record_run({"run_id": run}, {"at": datetime.now(UTC).isoformat(), "launched_by": "chair"})
        assert store_lease.acquire(conn, run_lease.lease_name(run), f"run:{run}", datetime.now(UTC).isoformat(), 3600).ok
    finally:
        conn.close()
    # `cox route release` is a process of its own: it reads the leases afresh, not this process's two-second snapshot.
    run_store._lease_rows.cache_clear()


@pytest.mark.parametrize("live", ["demo-56", "demo.p1-3"])
def test_end_to_end_route_release_waits_for_a_live_run_and_names_how_to_stop_it(ws, capsys, live):
    """Regression: a release while a run was live marked the fresh start below attempts that run had yet to record.
    `cox route release` is refused, as `route edit --state ready` is, and both name `cox runs stop`."""
    _held_by_the_chair(ws)
    _live_run(ws, live)
    capsys.readouterr()

    assert cli.main(["route", "release", "demo"]) == 2
    assert (
        f"release: refused: {live} is live on demo; stop it with `cox runs stop {live}`, or let it finish, before releasing demo"
    ) in capsys.readouterr().out
    assert _held(ws)["held"] == "true" and "fresh_after_run" not in _held(ws)


def test_end_to_end_route_release_goes_ahead_while_only_another_initiative_runs(ws):
    _held_by_the_chair(ws)
    _live_run(ws, "demonstration-4")

    assert cli.main(["route", "release", "demo"]) == 0
    assert "held" not in _held(ws) and _held(ws)["fresh_after_run"] == {"demo": 55}


def test_live_refusal_names_the_run_and_the_command_that_stops_it():
    assert route_guard.live_refusal("demo-9", "demo", "releasing demo") == (
        "demo-9 is live on demo; stop it with `cox runs stop demo-9`, or let it finish, before releasing demo"
    )
