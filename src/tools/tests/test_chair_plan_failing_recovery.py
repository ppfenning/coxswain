from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


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
        "last_housekeeping_at": _NOW.isoformat(),
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _approved(initiative: str, phase: str = "p1", run: str = "") -> dict:
    return {
        "id": f"{initiative}-done", "initiative": initiative, "repo": f"repo-{initiative}", "phase": phase,
        "phase_done": True, "needs": [], "run": run, "needs_fetch": False,
    }


def _initiative(id: str, *phases: str) -> dict:
    ready = [{"id": f"{id}-{p}", "phase": p, "needs": [], "requires": []} for p in phases]
    return {"id": id, "repo": f"repo-{id}", "started": False, "ready_tasks": ready, "landed": set()}


def _launched(actions: list[dict]) -> set[str]:
    return {a["initiative"] for a in actions if a["kind"] in {"launch_epic", "relaunch"}}


def test_a_failing_land_holds_its_phase_and_the_initiative_launches_its_other_phases():
    # Both lands are planned this tick. Only p1's ready work is held: one initiative has p3 work to launch, the other none.
    facts = _facts(
        approved=[_approved("held", run="held-1"), _approved("other", run="other-1")],
        initiatives=[_initiative("held", "p1"), _initiative("other", "p1", "p3")],
        land_fail_streaks=[{"initiative": "other", "phase": "p1", "kind": "land_phase", "reason": "checks red", "ticks": 1}],
    )
    actions = plan_tick(facts, _NOW)
    assert sorted(a["initiative"] for a in actions if a["kind"] == "land_phase") == ["held", "other"]
    assert _launched(actions) == {"other"}


def test_a_land_failing_three_ticks_running_stops_with_one_needs_chair_and_the_initiative_launches():
    streak = {"initiative": "other", "phase": "p1", "kind": "land_phase", "reason": "checks red", "ticks": 3}
    facts = _facts(approved=[_approved("other", run="other-1")], initiatives=[_initiative("other", "p1")], land_fail_streaks=[streak])
    actions = plan_tick(facts, _NOW)
    assert not [a for a in actions if a["kind"] in {"land_phase", "partial_land", "merge_level"}]
    stuck = [a for a in actions if a["kind"] == "needs_chair" and a.get("cause") == "stuck_land"]
    assert len(stuck) == 1 and "checks red" in stuck[0]["reason"]
    assert _launched(actions) == {"other"}


def test_an_approved_task_with_no_run_for_three_ticks_is_requeued_and_raises_no_no_run():
    facts = _facts(approved=[_approved("other")], initiatives=[_initiative("other")], no_run_streaks={"other-done": 3})
    actions = plan_tick(facts, _NOW)
    assert [(a["kind"], a["task_id"]) for a in actions if a["kind"] == "requeue_task"] == [("requeue_task", "other-done")]
    assert not [a for a in actions if a["kind"] == "needs_chair" and a.get("cause") == "no_run"]


def test_under_three_ticks_the_no_run_needs_chair_stands_and_nothing_is_requeued():
    facts = _facts(approved=[_approved("other")], initiatives=[_initiative("other")], no_run_streaks={"other-done": 2})
    actions = plan_tick(facts, _NOW)
    assert not [a for a in actions if a["kind"] == "requeue_task"]
    assert [a["initiative"] for a in actions if a["kind"] == "needs_chair" and a.get("cause") == "no_run"] == ["other"]
