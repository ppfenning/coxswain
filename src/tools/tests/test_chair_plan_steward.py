from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick, plan_tick_held
from agent_tools.chair_plan_recover import MAIN_RED_CAUSE
from agent_tools.chair_plan_routine import routine_only
from tests.test_chair_plan import _approved, _blocked, _facts, _initiative, _kinds, _lease, _unstarted

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
STANDBY = {"kind": "standby", "holder": "other", "host": "elsewhere", "epoch": 7}
STEWARD_RELAUNCH = {
    "kind": "relaunch", "initiative": "a", "epic": "a", "topic": "a", "fields": {"signature": "s1"}, "epoch": 7,
}
MAIN_RED = {"task_id": "a-q", "initiative": "a", "cause": MAIN_RED_CAUSE, "harness_failures": 0}


def _epic(*tasks: tuple[str, str], merged: bool = False, exited: bool = True) -> dict:
    levels = [{"tasks": [{"id": i, "state": s} for i, s in tasks], "merged": merged, "phase": "p1"}]
    return {
        "epic_id": "a", "branch_head": "h1", "levels": levels, "exited": exited, "signature": "s1", "reported": (),
        "land_at": "ticket",
    }


def _steward(*epics: dict, enabled: bool = True) -> dict:
    return {"enabled": enabled, "epics": list(epics)}


def _facts_for(*epics: dict, **overrides) -> dict:
    base = {"initiatives": [_initiative("a")], "run_exited": {"a": True}, "steward": _steward(*epics)}
    return _facts(**{**base, **overrides})


def _without_steward(facts: dict) -> dict:
    return {k: v for k, v in facts.items() if k != "steward"}


PLANNER_RELAUNCH_BEHIND = {"kind": "relaunch", "initiative": "a", "epoch": 7, "after_steward": True}


def test_an_approved_unmerged_level_plans_a_merge_then_the_planner_relaunch_tagged_after_steward():
    facts = _facts_for(_epic(("a-t", "approved")))
    assert _kinds(plan_tick(_without_steward(facts))) == ["clear_branches", "relaunch"]
    assert plan_tick(facts) == [
        {"kind": "merge_level", "epic": "a", "topic": "a", "fields": {"head": "h1", "level": "0"}, "level": 0, "epoch": 7},
        PLANNER_RELAUNCH_BEHIND,
    ]


def test_an_epic_whose_steward_relaunches_itself_has_no_tagged_relaunch():
    actions = plan_tick(_facts_for(_epic(("a-t", "ready"))))
    assert actions == [STEWARD_RELAUNCH]
    assert not any(a.get("after_steward") for a in actions)


def test_a_quarantined_epic_with_an_approved_level_plans_no_relaunch():
    actions = plan_tick(_facts_for(_epic(("a-q", "quarantined"), ("a-t", "approved")), quarantines=[MAIN_RED]))
    assert [a for a in actions if a["kind"] == "relaunch"] == []


def test_an_exited_epic_with_ready_tasks_plans_one_relaunch_from_the_steward_only():
    facts = _facts_for(_epic(("a-t", "ready")))
    assert _kinds(plan_tick(_without_steward(facts))) == ["clear_branches", "relaunch"]
    assert plan_tick(facts) == [STEWARD_RELAUNCH]


def test_a_quarantine_the_planner_would_relaunch_for_plans_one_needs_attention_event_and_no_relaunch():
    facts = _facts_for(_epic(("a-q", "quarantined"), ("a-t", "ready")), quarantines=[MAIN_RED])
    assert "relaunch" in _kinds(plan_tick(_without_steward(facts)))
    actions = plan_tick(facts)
    assert [a for a in actions if a["kind"] == "relaunch"] == []
    assert [a for a in actions if a["kind"] == "steward_event"] == [
        {
            "kind": "steward_event", "epic": "a", "topic": "a", "fields": {"task": "a-q"}, "event": "needs_attention",
            "epoch": 7,
        }
    ]


def _relaunches(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a["kind"] == "relaunch"]


def test_a_waiting_initiative_gets_no_steward_relaunch():
    facts = _facts_for(_epic(("a-t", "ready")), initiatives=[_blocked("a")])
    assert _relaunches(plan_tick(facts)) == []
    assert [a["cause"] for a in plan_tick(facts)] == ["waiting on z"]
    assert plan_tick(facts) == plan_tick(_without_steward(facts))


def test_a_remote_unfetched_initiative_gets_no_steward_relaunch():
    facts = _facts_for(_epic(("a-t", "ready")), remote_unfetched={"a": "box"})
    assert _relaunches(plan_tick(_facts_for(_epic(("a-t", "ready"))))) != []
    assert _relaunches(plan_tick(facts)) == []
    assert plan_tick(facts) == plan_tick(_without_steward(facts))


def test_an_initiative_in_a_landing_repo_gets_no_steward_relaunch():
    landing = [{"initiative": "x", "phase": "p", "repo": "r1"}]
    facts = _facts_for(_epic(("a-t", "ready")), initiatives=[{**_initiative("a"), "repo": "r1"}], landing=landing)
    assert _relaunches(plan_tick(facts)) == []
    assert plan_tick(facts) == plan_tick(_without_steward(facts))


def test_a_lost_run_on_a_handled_epic_plans_one_steward_relaunch_where_the_planner_planned_a_pair():
    # A lost run has no exit record, so the epic is not exited here; the host's silence is the evidence.
    facts = _facts_for(_epic(("a-t", "ready"), exited=False), lost_runs={"a": "a-1"}, run_exited={"a": False})
    disabled = plan_tick(_without_steward(facts))
    assert _kinds(disabled) == ["mark_lost", "clear_branches", "relaunch"]
    assert _kinds(plan_tick(facts)) == ["mark_lost", "relaunch"]
    assert _relaunches(plan_tick(facts)) == [STEWARD_RELAUNCH]


def test_a_lost_run_whose_epic_has_an_approved_level_merges_first_and_relaunches_behind_the_merge():
    facts = _facts_for(_epic(("a-t", "approved"), exited=False), lost_runs={"a": "a-1"}, run_exited={"a": False})
    actions = plan_tick(facts)
    assert _kinds(actions) == ["mark_lost", "merge_level", "relaunch"]
    assert actions[-1]["after_steward"] is True


def test_a_lost_run_with_a_quarantine_gets_the_event_and_no_relaunch_from_either_path():
    facts = _facts_for(
        _epic(("a-q", "quarantined"), ("a-t", "ready"), exited=False),
        lost_runs={"a": "a-1"}, run_exited={"a": False}, quarantines=[MAIN_RED],
    )
    assert "relaunch" in _kinds(plan_tick(_without_steward(facts)))
    assert _kinds(plan_tick(facts)) == ["mark_lost", "steward_event"]


def test_a_relaunch_the_planner_does_not_see_is_not_planned_even_when_the_steward_wants_it():
    # The planner sees no ready task; the steward sees one. The planner's gate is the veto, so nothing relaunches.
    empty = {**_initiative("a"), "ready_tasks": []}
    facts = _facts_for(_epic(("a-t", "ready")), initiatives=[empty])
    assert plan_tick(facts) == plan_tick(_without_steward(facts)) == []


def test_a_relaunch_the_launch_cap_refuses_is_dropped_as_the_planner_drops_its_own():
    stop = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 0, "go_degraded": False}
    facts = _facts_for(_epic(("a-t", "ready")), limits=stop)
    assert _relaunches(plan_tick(facts)) == []
    assert plan_tick(facts) == plan_tick(_without_steward(facts))


def test_a_handled_epic_that_has_not_started_still_launches():
    facts = _facts_for(_epic(("a-t", "ready"), exited=False), initiatives=[_unstarted("a")])
    assert _kinds(plan_tick(facts)) == ["launch_epic"]
    assert plan_tick(facts) == plan_tick(_without_steward(facts))


def test_a_handled_started_epic_with_a_live_run_is_not_launched_by_fill():
    facts = _facts_for(_epic(("a-t", "ready"), exited=False), run_exited={"a": False})
    assert plan_tick(facts) == []


def test_an_initiative_the_steward_does_not_handle_keeps_the_planner_relaunch():
    facts = _facts(initiatives=[_initiative("b")], run_exited={"b": True}, steward=_steward(_epic(("a-t", "approved"))))
    assert [(a["kind"], a.get("initiative", a.get("epic"))) for a in plan_tick(facts)] == [
        ("clear_branches", "b"), ("relaunch", "b"), ("merge_level", "a"),
    ]


def test_a_disabled_steward_plans_what_an_absent_key_plans():
    facts = _facts_for(_epic(("a-t", "approved")))
    assert plan_tick({**facts, "steward": _steward(_epic(("a-t", "approved")), enabled=False)}) == plan_tick(
        _without_steward(facts)
    )


def test_an_absent_steward_key_plans_the_same_as_before():
    facts = _facts(
        initiatives=[_initiative("a"), _initiative("b")],
        run_exited={"a": True, "b": True},
        approved=[_approved("t1")],
        quarantines=[{"task_id": "c-q", "initiative": "c", "cause": "scope", "harness_failures": 0}],
    )
    assert plan_tick(facts) == [
        {"kind": "land_phase", "initiative": "x", "phase": "p", "repo": "r", "run": "x-1", "epoch": 7, "task_runs": {"t1": "x-1"}},
        {"kind": "needs_chair", "initiative": "c", "task_id": "c-q", "cause": "scope", "epoch": 7},
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "b", "epoch": 7},
        {"kind": "relaunch", "initiative": "b", "epoch": 7},
    ]


def test_a_hard_stop_keeps_the_steward_merge_and_event_and_plans_no_relaunch_and_no_gate_run():
    stop = {"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}

    def kinds(*tasks: tuple[str, str], merged: bool = False, **more) -> list[str]:
        return _kinds(plan_tick(_facts_for(_epic(*tasks, merged=merged), limits=stop, **more)))

    assert kinds(("a-t", "ready")) == []
    assert kinds(("a-t", "approved")) == ["merge_level"]
    assert kinds(("a-t", "done"), merged=True) == []
    assert kinds(("a-q", "quarantined"), ("a-t", "ready"), quarantines=[MAIN_RED]) == ["steward_event"]


def test_a_finished_epic_plans_the_gate_run_then_the_tagged_relaunch_when_there_is_no_hard_stop():
    facts = _facts_for(_epic(("a-t", "done"), merged=True))
    assert plan_tick(facts) == [
        {"kind": "run_gate", "epic": "a", "topic": "a", "fields": {"head": "h1"}, "epoch": 7},
        PLANNER_RELAUNCH_BEHIND,
    ]


def test_the_routine_filter_keeps_the_three_steward_kinds():
    actions = [{"kind": k} for k in ("merge_level", "launch_epic", "run_gate", "retry", "steward_event")]
    assert _kinds(routine_only(actions)) == ["merge_level", "run_gate", "steward_event"]


def test_a_lent_lease_still_plans_the_steward_actions_before_standby():
    facts = _facts_for(_epic(("a-t", "approved")), lease=_lease(lent=True))
    assert plan_tick_held(facts, NOW).actions == [
        {"kind": "merge_level", "epic": "a", "topic": "a", "fields": {"head": "h1", "level": "0"}, "level": 0, "epoch": 7},
        PLANNER_RELAUNCH_BEHIND,
        STANDBY,
    ]
