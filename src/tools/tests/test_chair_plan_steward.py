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
        {
            "kind": "merge_level", "epic": "a", "topic": "a", "fields": {"head": "h1", "level": "0"}, "level": 0,
            "land_at": "ticket", "epoch": 7,
        },
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


def test_an_initiative_being_landed_gets_no_steward_relaunch():
    landing = [{"initiative": "a", "phase": "p", "repo": "r1"}]
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
    assert _kinds(actions) == ["merge_level", "mark_lost", "relaunch"]
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
        ("merge_level", "a"), ("clear_branches", "b"), ("relaunch", "b"),
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
        {
            "kind": "merge_level", "epic": "a", "topic": "a", "fields": {"head": "h1", "level": "0"}, "level": 0,
            "land_at": "ticket", "epoch": 7,
        },
        PLANNER_RELAUNCH_BEHIND,
        STANDBY,
    ]


def _task(id: str, run: str, *, fetch: bool = False) -> dict:
    return {**_approved(id), "initiative": "a", "phase": "p1", "run": run, "needs_fetch": fetch}


def _landing_facts(land_at: str, *tasks: dict, steward: bool = True) -> dict:
    epic = {**_epic(*[(t["id"], "approved") for t in tasks]), "land_at": land_at}
    facts = _facts_for(epic, approved=list(tasks))
    return facts if steward else _without_steward(facts)


def _merges(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a["kind"] == "merge_level"]


def _lands(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a["kind"] in ("land_phase", "partial_land")]


def test_a_land_at_level_merge_carries_repo_phase_run_task_runs_and_fetch_runs():
    [merge] = _merges(plan_tick(_landing_facts("level", _task("a-t1", "a-2", fetch=True), _task("a-t2", "a-2", fetch=True))))
    assert merge | {"epoch": 0} == {
        "kind": "merge_level", "epic": "a", "topic": "a", "fields": {"head": "h1", "level": "0"}, "level": 0,
        "land_at": "level", "repo": "r", "phase": "p1", "run": "a-2", "task_runs": {"a-t1": "a-2", "a-t2": "a-2"},
        "fetch_runs": ["a-2"], "epoch": 0,
    }


def test_a_land_at_task_merge_carries_the_same_inputs_with_no_fetch_runs_when_fetched():
    [merge] = _merges(plan_tick(_landing_facts("task", _task("a-t1", "a-2"))))
    assert (merge["land_at"], merge["repo"], merge["phase"], merge["run"]) == ("task", "r", "p1", "a-2")
    assert (merge["task_runs"], merge["fetch_runs"]) == ({"a-t1": "a-2"}, [])


def test_a_land_at_ticket_merge_carries_no_land_inputs():
    [merge] = _merges(plan_tick(_landing_facts("ticket", _task("a-t1", "a-2"))))
    assert merge["land_at"] == "ticket"
    assert not {"repo", "phase", "run", "task_runs", "fetch_runs"} & merge.keys()


def test_a_split_phase_in_two_runs_lands_from_the_newest_and_lists_both_in_task_runs():
    tasks = (_task("a-t1", "a-2"), _task("a-t2", "a-10", fetch=True), _task("a-t3", "a-3", fetch=True))
    [merge] = _merges(plan_tick(_landing_facts("level", *tasks)))
    assert merge["run"] == "a-10"
    assert merge["task_runs"] == {"a-t1": "a-2", "a-t2": "a-10", "a-t3": "a-3"}
    assert merge["fetch_runs"] == ["a-10", "a-3"]


def test_a_task_with_an_unknown_run_leaves_the_merge_without_a_run():
    [merge] = _merges(plan_tick(_landing_facts("level", _task("a-t1", "a-2"), _task("a-t2", ""))))
    assert "run" not in merge
    assert (merge["repo"], merge["phase"], merge["task_runs"]) == ("r", "p1", {"a-t1": "a-2", "a-t2": ""})


def test_a_tick_plans_one_land_for_the_phase_the_steward_merges_not_two():
    tasks = (_task("a-t1", "a-2", fetch=True), _task("a-t2", "a-2", fetch=True))
    assert [a["kind"] for a in _lands(plan_tick(_landing_facts("level", *tasks, steward=False)))] == ["land_phase"]
    actions = plan_tick(_landing_facts("level", *tasks))
    assert len(_merges(actions)) == 1
    assert _lands(actions) == []
    assert [a for a in actions if a["kind"] == "fetch"] == []


def test_a_ticket_merge_leaves_the_planner_land_in_place():
    tasks = (_task("a-t1", "a-2"),)
    unhandled = _lands(plan_tick(_landing_facts("ticket", *tasks, steward=False)))
    assert _lands(plan_tick(_landing_facts("ticket", *tasks))) == unhandled


def _merge_and_planner_land_facts(**overrides) -> dict:
    """A land_at level merge for phase p1 of repo r, plus the planner's own land for phase p of the same repo."""
    facts = _landing_facts("level", _task("a-t1", "a-2", fetch=True))
    return {**facts, "approved": [*facts["approved"], _approved("x-t")], **overrides}


def test_a_steward_merge_is_planned_before_the_planner_land_of_another_phase_in_the_same_repo():
    kinds = _kinds(plan_tick(_merge_and_planner_land_facts()))
    assert kinds.count("land_phase") == 1
    assert kinds.index("merge_level") < kinds.index("land_phase")


def test_a_hard_stop_plans_the_steward_merge_before_the_planner_land():
    stop = {"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}
    kinds = _kinds(plan_tick(_merge_and_planner_land_facts(limits=stop)))
    assert kinds.count("land_phase") == 1
    assert kinds.index("merge_level") < kinds.index("land_phase")
