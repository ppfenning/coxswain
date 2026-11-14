from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_LIMITS = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}
_REASON = "released by the approval hold while merged on its phase branch; restoring to approved"


def _facts(**extra: object) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": _LIMITS,
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
        "last_housekeeping_at": _NOW.isoformat(),
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **extra}  # type: ignore[return-value]


def _row(**landing: object) -> dict:
    base = {
        "id": "A-t", "initiative": "A", "repo": "r", "phase": "p1", "phase_done": False, "needs": [],
        "run": "run-1", "needs_fetch": False, "merged": False,
    }
    return {**base, **landing}


def _of(plan: list[dict], kind: str) -> list[dict]:
    return [a for a in plan if a["kind"] == kind]


def test_a_wrongly_released_entry_yields_one_restore_approvals_action():
    plan = plan_tick(_facts(wrongly_released=[{"initiative": "A", "task_id": "A-t"}]), _NOW)
    restores = _of(plan, "restore_approvals")
    assert [(a["initiative"], a["task_ids"], a["reason"]) for a in restores] == [("A", ["A-t"], _REASON)]


def test_two_entries_of_one_initiative_yield_one_action_with_both_ids_in_facts_order():
    entries = [{"initiative": "A", "task_id": "A-2"}, {"initiative": "A", "task_id": "A-1"}]
    restores = _of(plan_tick(_facts(wrongly_released=entries), _NOW), "restore_approvals")
    assert [(a["initiative"], a["task_ids"]) for a in restores] == [("A", ["A-2", "A-1"])]


def test_entries_of_two_initiatives_yield_one_action_each():
    entries = [{"initiative": "B", "task_id": "B-1"}, {"initiative": "A", "task_id": "A-1"}]
    restores = _of(plan_tick(_facts(wrongly_released=entries), _NOW), "restore_approvals")
    assert [(a["initiative"], a["task_ids"]) for a in restores] == [("B", ["B-1"]), ("A", ["A-1"])]


def test_no_wrongly_released_key_yields_no_restore():
    assert _of(plan_tick(_facts(), _NOW), "restore_approvals") == []


def test_an_empty_wrongly_released_list_yields_no_restore():
    assert _of(plan_tick(_facts(wrongly_released=[]), _NOW), "restore_approvals") == []


def test_a_long_held_task_is_not_released_and_every_wrongly_released_entry_is_restored():
    facts = _facts(
        approved=[_row()],
        running=[{"id": "A", "repo": "r", "surfaces": ["a.py"], "live": False}],
        approval_holds=[{"initiative": "A", "task_id": "A-t", "ticks": 50, "surfaces": ["a.py"]}],
        wrongly_released=[{"initiative": "A", "task_id": "A-t"}, {"initiative": "A", "task_id": "A-old"}],
    )
    plan = plan_tick(facts, _NOW)
    assert _of(plan, "release_approvals") == []
    assert [a["task_ids"] for a in _of(plan, "restore_approvals")] == [["A-t", "A-old"]]


def test_a_ticket_landing_hold_merged_to_its_phase_branch_is_still_not_released():
    facts = _facts(
        approved=[_row(branch_merged=True, land_at="ticket")],
        running=[{"id": "A", "repo": "r", "surfaces": ["a.py"], "live": False}],
        approval_holds=[{"initiative": "A", "task_id": "A-t", "ticks": 50, "surfaces": ["a.py"]}],
    )
    plan = plan_tick(facts, _NOW)
    assert _of(plan, "release_approvals") == []
    assert _of(plan, "restore_approvals") == []
