"""The 2026-11-04 stall: a decomposed plan whose phase-2 task needs a done phase-1 task was never relaunched.

The task rows are exactly what `queue_rows.plan_to_rows` emits, so the relaunch test fails if decompose stops writing `ready`.
"""
from agent_tools import queue_rows
from agent_tools.chair_facts import initiative_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_read_docket import docket_from_rows
from agent_tools.chair_types import Facts

_PLAN = {
    "id": "init-a", "title": "A", "body": "about A",
    "phases": [{"id": "p1", "goal": "g1"}, {"id": "p2", "goal": "g2"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "title": "T1", "body": "do one", "needs": [], "surfaces": ["a.py"]},
        {"id": "t2", "phase": "p2", "title": "T2", "body": "do two", "needs": ["t1"], "surfaces": ["b.py"]},
    ],
}

_RELAUNCH = {"kind": "relaunch", "initiative": "init-a", "epoch": 7}


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": None,
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _decomposed_rows() -> list[dict]:
    """The task rows `plan_to_rows` emits, with t1 marked done and unleased keys added; t2 keeps its emitted state.

    The initiative row is left out: `docket_from_rows` reads task rows only, and `started` comes from the done t1.
    """
    _head, t1, t2 = queue_rows.plan_to_rows(_PLAN, "I1")
    lease = {"holder": None, "epoch": 0, "expires_at": ""}
    return [{**t1, "state": "done", **lease}, {**t2, **lease}]


def _docket(rows: list[dict]) -> list[dict]:
    return docket_from_rows(rows, "2026-11-04T00:00:00Z")


def _actions(rows: list[dict]) -> list[dict]:
    facts = _facts(initiatives=initiative_facts({"initiatives": _docket(rows)}, set()), run_exited={"init-a": True})
    return plan_tick(facts)


def test_a_decomposed_second_phase_task_as_plan_to_rows_writes_it_plans_a_relaunch():
    rows = _decomposed_rows()
    assert [[t["id"] for t in i["ready_tasks"]] for i in _docket(rows)] == [["t2"]]
    assert _RELAUNCH in _actions(rows)


def test_the_same_board_with_the_second_phase_task_todo_has_no_ready_task_and_plans_no_relaunch():
    done, p2 = _decomposed_rows()
    rows = [done, {**p2, "state": "todo"}]
    assert _docket(rows) == []  # no ready task, so the docket omits the initiative
    assert _RELAUNCH not in _actions(rows)
