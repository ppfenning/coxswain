"""The chair's revert of a phase, end to end: the quarantine the revert writes, read back by the next tick's real facts.

A land that turned main red is reverted and its phase quarantined, so no relaunch stacks onto a red main. The next tick
must see that quarantine as main_red, never as a task to triage: a triage request moves the task back to ready while
main may still be red. The reverted tasks stay quarantined until a person releases them, which the revert's needs_chair
names; the initiative's relaunch pair (until two reverts in a row) builds its other ready work only. And the quarantine
takes only the tasks that were part of the land; a dropped task, or one still building under a live lane, is left as it was.
"""

import dataclasses
import json
import sys
from datetime import UTC, datetime

import pytest

from agent_tools import chair_exec, chair_facts, chair_revert_ports, cli, run_store
from agent_tools.chair_plan import plan_tick
from harness import epic
from harness.store_migrate import open_store
from harness.store_write import Store

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
# The reverted phase p1: two tasks that were part of its land, one dropped, one building again. p2 is another phase.
_ROWS = (("t1", "p1", "approved"), ("t2", "p1", "done"), ("t3", "p1", "dropped"), ("t4", "p1", "in_progress"), ("t5", "p2", "ready"))


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
    (tmp_path / "runs").mkdir()
    open_store(run_store.store_url_for(tmp_path / "runs"), "2026-01-01T00:00:00Z").close()
    initiative = {"kind": "initiative", "initiative": "demo", "task_id": "demo", "phase": "", "state": "todo"}
    tasks = [{"kind": "task", "initiative": "demo", "task_id": t, "phase": p, "state": s} for t, p, s in _ROWS]
    for row in (initiative, *tasks):
        common = {"needs": [], "surfaces": [], "title": row["task_id"], "body": f"Build {row['task_id']}.", "extra": {}}
        assert run_store.upsert_row(tmp_path / "runs", {**common, **row})
    _ended_run(tmp_path, "demo-3")  # the run that built p1 and landed it
    return tmp_path


def _gathered(ws) -> dict:
    """The facts a real tick gathers: `gather_facts` over the chair's own `cli._chair_run_deps` for this workspace."""
    profile = {"workspace_dir": str(ws), "harness_dir": str(ws / "harness")}
    deps = cli._chair_run_deps(ws / "runs", profile, "chair", 1, "h", True, lambda line: None, ws / "profile.yaml", "store")
    return chair_facts.gather_facts(deps.facts_deps, _NOW)


def _deps(ws, recorded: list) -> chair_exec.Deps:
    """The chair's own edge bundle, with the revert fields `cox chair run` wires (`cli._revert_land_deps`)."""
    deps = chair_exec.edge_deps(ws / "runs", ws, "chair", 1, run_id=lambda a: "", repo_for=lambda a: "",
                                record=recorded.append, host="h", harness_python=sys.executable)
    revert = cli._revert_land_deps(
        ws / "runs", "chair", chair_revert_ports.bounded(chair_exec.run_argv), lambda: [], recorded.append,
    )
    return dataclasses.replace(deps, **revert)


def _revert(ws) -> dict:
    # The repo does not exist, so the revert itself fails for real; the phase is quarantined whatever the revert did.
    action = {"kind": "revert_land", "initiative": "demo", "phase": "p1", "repo": str(ws / "missing"), "pr": 7,
              "commit": "abc123", "reason": "smoke: main went red", "epoch": 0}
    return chair_exec._execute(action, _deps(ws, []), {})


def _states(ws) -> dict[str, str]:
    return {r["task_id"]: r["state"] for r in run_store.read_queue(ws / "runs", "demo", kind="task")}


def test_a_revert_quarantines_only_the_tasks_of_the_land_and_leaves_the_rest(ws):
    result = _revert(ws)
    assert "quarantine not recorded" not in result["reason"], result["reason"]
    assert "release each: cox route edit t1 --initiative demo --state ready; cox route edit t2 --initiative demo --state ready" in result["reason"]
    assert _states(ws) == {"t1": "quarantined", "t2": "quarantined", "t3": "dropped", "t4": "in_progress", "t5": "ready"}


def _ended_run(ws, run: str) -> None:
    """One run as the epic records it: the launch's runs row, then its exit record."""
    conn = open_store(run_store.store_url_for(ws / "runs"), "2026-01-01T00:00:00Z")
    try:
        store = Store(conn)
        store.record_run({"run_id": run})
        epic.record_run_exit(store, run, "ok", epic.StoreRetry())
    finally:
        conn.close()


def _earlier_failure(ws, run: str) -> None:
    """t1 failed a build in `run`: the run, ended, and its task record naming a cause."""
    _ended_run(ws, run)
    record = ws / "runs" / run / "tasks" / "p1" / "t1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"cause": "code", "reason": "an earlier failed build", "initiative": "demo"}), encoding="utf-8")


def _next_tick(ws) -> tuple[dict[str, str], list[dict]]:
    """The tick after the revert: its lease taken first, as a fresh chair does, then the real facts and the real plan."""
    assert plan_tick(_gathered(ws), _NOW) == [{"kind": "take_lease", "epoch": 0}]
    assert chair_exec._execute({"kind": "take_lease", "epoch": 0}, _deps(ws, []), {})["status"] == "done"
    facts = _gathered(ws)
    return {q["task_id"]: q["cause"] for q in facts["quarantines"] if q["initiative"] == "demo"}, plan_tick(facts, _NOW)


@pytest.mark.parametrize("earlier", [False, True], ids=["no-attempt", "an-earlier-code-failure"])
def test_the_next_tick_reads_the_reverted_phase_as_main_red_and_never_sends_it_back_to_ready(ws, earlier):
    if earlier:
        _earlier_failure(ws, "demo-2")
    _revert(ws)

    causes, planned = _next_tick(ws)

    assert causes == {"t1": "main_red", "t2": "main_red"}, causes
    demo = [(a["kind"], a.get("task_id")) for a in planned if a.get("initiative") == "demo"]
    # main_red leaves the initiative's relaunch pair alone, and it builds p2's ready task; t1 and t2 stay quarantined
    # for a person to release. The quarantine itself plans no triage (back to ready) and no needs_chair.
    assert ("request_triage", "t1") not in demo and ("request_triage", "t2") not in demo, planned
    assert not [k for k, t in demo if k == "needs_chair" and t in ("t1", "t2")], planned
    assert [k for k, _ in demo if k in ("clear_branches", "relaunch")] == ["clear_branches", "relaunch"], planned
    assert _states(ws)["t1"] == "quarantined"


def test_an_attempt_from_a_run_after_the_revert_outdates_its_mark(ws):
    _earlier_failure(ws, "demo-2")
    _revert(ws)
    _earlier_failure(ws, "demo-4")  # the relaunch after the revert failed t1 again, in a run the mark predates

    causes, _ = _next_tick(ws)

    assert causes["t1"] == "code"
    assert causes["t2"] == "main_red"

