from agent_tools.chair_plan import _without_gated_lands, plan_tick, plan_tick_held
from agent_tools.chair_types import Facts
from agent_tools.ci_gate import CiGate
from agent_tools.release_gate import deferred_initiatives


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


def _started(id: str) -> dict:
    return {"id": id, "started": True, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


def _unstarted(id: str) -> dict:
    return {**_started(id), "started": False}


def _approved(initiative: str) -> dict:
    return {
        "id": f"{initiative}-t", "initiative": initiative, "repo": "r", "phase": "p", "phase_done": True, "needs": [],
        "run": f"{initiative}-1", "needs_fetch": False,
    }


def _for(actions: list[dict], initiative: str) -> list[str]:
    return [a["kind"] for a in actions if initiative in (a.get("initiative"), a.get("epic"))]


def test_a_held_initiative_with_a_ready_task_gets_no_launch_epic():
    facts = _facts(initiatives=[_unstarted("h"), _unstarted("o")], held_initiatives=["h"])
    actions = plan_tick(facts)
    assert _for(actions, "h") == []
    assert _for(actions, "o") == ["launch_epic"]


def test_a_held_started_initiative_that_would_relaunch_gets_no_relaunch_or_clear():
    facts = _facts(initiatives=[_started("h"), _started("o")], run_exited={"h": True, "o": True}, held_initiatives=["h"])
    actions = plan_tick(facts)
    assert _for(actions, "h") == []
    assert _for(actions, "o") == ["clear_branches", "relaunch"]


def test_a_held_initiative_with_an_approved_phase_gets_no_land():
    facts = _facts(approved=[_approved("h"), _approved("o")], held_initiatives=["h"])
    actions = plan_tick(facts)
    assert _for(actions, "h") == []
    assert _for(actions, "o") == ["land_phase"]


def test_a_held_initiative_gets_no_land_under_a_hard_stop_either():
    limits = {"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}
    facts = _facts(limits=limits, approved=[_approved("h"), _approved("o")], held_initiatives=["h"])
    assert [(a["kind"], a["initiative"]) for a in plan_tick(facts)] == [("land_phase", "o")]


def test_the_same_facts_without_the_held_entry_launch_it_on_the_next_tick():
    initiatives = [_unstarted("h")]
    held = plan_tick(_facts(initiatives=initiatives, held_initiatives=["h"]))
    released = plan_tick(_facts(initiatives=initiatives, held_initiatives=[]))
    assert _for(held, "h") == []
    assert _for(released, "h") == ["launch_epic"]


def test_the_same_facts_without_the_held_entry_relaunch_and_land_it():
    facts = {"initiatives": [_started("h")], "approved": [_approved("h")], "run_exited": {"h": True}}
    held = plan_tick(_facts(**facts, held_initiatives=["h"]))
    released = plan_tick(_facts(**facts))
    assert _for(held, "h") == []
    assert "land_phase" in _for(released, "h")
    assert "relaunch" in _for(released, "h")


def test_a_deferred_initiative_gets_no_launch_while_an_identical_unlisted_one_does():
    facts = _facts(initiatives=[_unstarted("d"), _unstarted("u")], deferred_initiatives={"d": "9.9.0"})
    actions = plan_tick(facts)
    assert _for(actions, "d") == []
    assert _for(actions, "u") == ["launch_epic"]


def test_a_retry_and_a_rescue_for_a_gated_initiative_are_dropped_while_its_needs_chair_stays(monkeypatch):
    recovering = [
        {"kind": "retry", "task_id": "r1", "initiative": "h"},
        {"kind": "rescue", "task_id": "r2", "initiative": "h"},
        {"kind": "needs_chair", "initiative": "h", "cause": "scope"},
        {"kind": "retry", "task_id": "r3", "initiative": "o"},
    ]
    monkeypatch.setattr("agent_tools.chair_plan.plan_recover", lambda facts, now=None: recovering)
    actions = plan_tick(_facts(held_initiatives=["h"]))
    assert _for(actions, "h") == ["needs_chair"]
    assert _for(actions, "o") == ["retry"]


def test_a_gated_initiative_is_not_reported_as_held_for_ci():
    facts = _facts(initiatives=[_started("h")], run_exited={"h": True}, held_initiatives=["h"])
    assert plan_tick_held(facts, None, CiGate(paused=False, reason="")).held_for_ci == ()


def test_a_land_with_no_initiative_key_stays_and_a_fetch_is_not_a_land():
    lands = [{"kind": "land_phase", "initiative": "h"}, {"kind": "fetch", "initiative": "h"}, {"kind": "revert"}]
    assert _without_gated_lands(lands, frozenset({"h"})) == lands[1:]  # type: ignore[arg-type]


def test_a_held_initiative_keeps_its_fetch_but_gets_no_land():
    approved = {**_approved("h"), "needs_fetch": True}
    assert _for(plan_tick(_facts(approved=[approved], held_initiatives=["h"])), "h") == ["fetch"]
    assert _for(plan_tick(_facts(approved=[approved])), "h") == ["fetch", "land_phase"]


def test_a_harness_quarantine_on_a_held_initiative_plans_no_retry_while_another_still_retries():
    quarantine = {"cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False}
    quarantines = [{**quarantine, "task_id": "q1", "initiative": "h"}, {**quarantine, "task_id": "q2", "initiative": "o"}]
    assert _for(plan_tick(_facts(quarantines=quarantines)), "h") == ["retry"]
    actions = plan_tick(_facts(quarantines=quarantines, held_initiatives=["h"]))
    assert _for(actions, "h") == []
    assert _for(actions, "o") == ["retry"]


def _row(initiative: str, target: str) -> dict:
    return {"kind": "initiative", "state": "queued", "task_id": initiative, "initiative": initiative, "extra": {"target_release": target}}


def test_a_target_release_above_the_current_version_defers_the_launch_until_the_version_reaches_it():
    rows = [_row("d", "2.0.0")]
    initiatives = [_unstarted("d"), _unstarted("u")]
    below = plan_tick(_facts(initiatives=initiatives, deferred_initiatives=deferred_initiatives(rows, "1.9.0")))
    reached = plan_tick(_facts(initiatives=initiatives, deferred_initiatives=deferred_initiatives(rows, "2.0.0")))
    assert (_for(below, "d"), _for(below, "u")) == ([], ["launch_epic"])
    assert sorted(a["initiative"] for a in reached if a["kind"] == "launch_epic") == ["d", "u"]
