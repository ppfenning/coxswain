from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_LIMITS = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}


def _facts(approved: list[dict], ticks: int) -> Facts:
    return {  # type: ignore[return-value]
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": _LIMITS,
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": approved,
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
        "running": [{"id": "A", "repo": "r", "surfaces": ["a.py"], "live": False}],
        "approval_holds": [{"initiative": "A", "task_id": "A-t", "ticks": ticks, "surfaces": ["a.py"]}],
    }


def _row(**landing: object) -> dict:
    base = {
        "id": "A-t", "initiative": "A", "repo": "r", "phase": "p1", "phase_done": False, "needs": [],
        "run": "run-1", "needs_fetch": False, "merged": False,
    }
    return {**base, **landing}


def _releases(plan: list[dict]) -> list[dict]:
    return [a for a in plan if a["kind"] == "release_approvals"]


def test_a_ticket_landing_hold_merged_to_its_phase_branch_is_not_released():
    plan = plan_tick(_facts([_row(branch_merged=True, land_at="ticket")], 50), _NOW)
    assert _releases(plan) == []


def test_a_level_landing_hold_merged_to_its_phase_branch_is_not_released():
    plan = plan_tick(_facts([_row(branch_merged=True, land_at="level")], 50), _NOW)
    assert _releases(plan) == []


def test_a_task_landing_hold_merged_nowhere_is_not_released_however_long_it_held():
    for ticks in (6, 50):
        plan = plan_tick(_facts([_row(branch_merged=False, land_at="task")], ticks), _NOW)
        assert _releases(plan) == []
