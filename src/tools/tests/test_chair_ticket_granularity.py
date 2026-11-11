from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_land import backoff_held
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
_LAUNCH = {"launch_epic", "relaunch"}
_LANDS = {"land_phase", "partial_land", "fetch"}


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {
            "hard_stop": False,
            "weekly_fraction": 0.1,
            "hard_stop_fraction": 0.9,
            "launch_cap": 5,
            "go_degraded": False,
        },
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {"a": True},
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


def _initiative(land_at: str = "ticket", ready: list[dict] | None = None) -> dict:
    ready = [{"id": "a-p2", "phase": "p2", "needs": []}] if ready is None else ready
    return {"id": "a", "repo": "r", "started": False, "ready_tasks": ready, "landed": set(), "land_at": land_at}


def _approved(task: str, phase: str) -> dict:
    return {
        "id": task,
        "initiative": "a",
        "repo": "r",
        "phase": phase,
        "phase_done": True,
        "needs": [],
        "run": "run-1",
        "needs_fetch": False,
        "merged": True,
    }


def _row(phase: str, *tasks: tuple[str, str]) -> dict:
    return {"initiative": "a", "phase": phase, "tasks": list(tasks)}


def _p1_facts(land_at: str = "ticket", **overrides) -> Facts:
    return _facts(
        initiatives=[_initiative(land_at)],
        approved=[_approved("a-p1", "p1")],
        phase_tasks=[_row("p1", ("a-p1", "merged"))],
        **overrides,
    )


def _kinds(actions: list[dict], kinds: set[str]) -> list[dict]:
    return [a for a in actions if a["kind"] in kinds]


def _complete(facts: Facts) -> Facts:
    epic = {
        "epic_id": "a", "branch_head": "h1", "levels": [], "exited": False, "signature": "",
        "reported": {"epic_complete:h1"}, "land_at": "ticket",
    }
    return {**facts, "steward": {"enabled": True, "epics": [epic]}}  # type: ignore[typeddict-item]


def test_a_non_last_phase_at_ticket_plans_no_land_and_the_initiative_still_launches() -> None:
    actions = plan_tick(_p1_facts(), _NOW)
    assert _kinds(actions, {"land_phase"}) == []
    assert [a["initiative"] for a in _kinds(actions, _LAUNCH)] == ["a"]


def test_a_ticket_phase_inside_a_refusal_backoff_holds_the_initiative_from_launches() -> None:
    refusal = {"initiative": "a", "phase": "p1", "run": "run-1", "attempts": 1, "last_refused_at": _NOW.isoformat()}
    facts = _complete(_p1_facts(land_refusals=[refusal]))
    assert backoff_held(facts, _NOW) == frozenset({"a"})
    assert _kinds(plan_tick(facts, _NOW), _LANDS | _LAUNCH) == []


def test_level_and_task_still_land_a_non_last_phase() -> None:
    for land_at in ("level", "task"):
        actions = plan_tick(_p1_facts(land_at), _NOW)
        assert [a["phase"] for a in _kinds(actions, {"land_phase"})] == ["p1"]
        assert _kinds(actions, {"deferred"}) == []


def test_a_quarantined_sibling_at_ticket_plans_no_land() -> None:
    facts = _facts(
        initiatives=[_initiative()],
        approved=[{**_approved("a-p1", "p1"), "phase_done": False}],
        phase_tasks=[_row("p1", ("a-p1", "approved"), ("a-q", "quarantined"))],
    )
    assert _kinds(plan_tick(facts, _NOW), _LANDS) == []
