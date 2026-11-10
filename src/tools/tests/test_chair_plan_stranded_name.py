from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _entry(cause: str, task_id: str = "t") -> dict:
    return {"task_id": task_id, "initiative": "i", "cause": cause, "harness_failures": 0}


def _facts(*quarantines: dict) -> Facts:
    return {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [{"id": "i", "started": False, "ready_tasks": [{"id": "i-t", "needs": []}], "landed": set()}],
        "quarantines": list(quarantines),
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
    }  # type: ignore[return-value]


def _unlaunched_reasons(*quarantines: dict) -> list[str]:
    actions = plan_tick(_facts(*quarantines), _NOW)
    assert not [a for a in actions if a["kind"] == "launch_epic"]
    return [a["reason"] for a in actions if a["kind"] == "unlaunched" and a["initiative"] == "i"]


def test_an_initiative_held_only_by_a_stranded_entry_reads_stranded_and_is_not_filled():
    reasons = _unlaunched_reasons(_entry("stranded"))
    assert len(reasons) == 1
    assert "stranded" in reasons[0]
    assert "quarantined" not in reasons[0]


def test_an_initiative_held_by_a_real_quarantine_reads_quarantined():
    reasons = _unlaunched_reasons(_entry("scope"))
    assert len(reasons) == 1
    assert "quarantined" in reasons[0]


def test_a_real_quarantine_beside_a_stranded_entry_reads_quarantined():
    reasons = _unlaunched_reasons(_entry("stranded", "s"), _entry("scope", "q"))
    assert len(reasons) == 1
    assert "quarantined" in reasons[0]
    assert "stranded" not in reasons[0]
