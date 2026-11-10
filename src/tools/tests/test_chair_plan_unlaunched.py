from datetime import UTC, datetime

from agent_tools.chair_plan import _unlaunched_actions, plan_tick, plan_tick_held
from tests.test_chair_plan import _facts, _initiative, _lease, _unstarted
from tests.test_chair_plan_steward import _epic, _steward

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RECENT = [{"at": "2026-10-07T11:50:00+00:00", "kind": "launch_epic"}, {"at": "2026-10-07T11:40:00+00:00", "kind": "relaunch"}]
QUARANTINE = {"task_id": "a-t", "initiative": "a", "cause": "scope", "harness_failures": 0}


def _unlaunched(facts) -> dict[str, str]:
    return {a["initiative"]: a["reason"] for a in plan_tick(facts, NOW) if a["kind"] == "unlaunched"}


def _surfaced(id: str, surfaces: list[str]) -> dict:
    return {**_unstarted(id), "repo": "r", "ready_surfaces": surfaces}


def test_eleven_ready_initiatives_with_two_launched_yield_nine_unlaunched_actions_with_reasons():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        initiatives=[_unstarted(f"i{n:02}") for n in range(11)],
    )
    actions = plan_tick(facts, NOW)
    launched = {a["initiative"] for a in actions if a["kind"] == "launch_epic"}
    left_out = [a for a in actions if a["kind"] == "unlaunched"]
    assert len(launched) == 2
    assert {a["initiative"] for a in left_out} == {f"i{n:02}" for n in range(11)} - launched
    assert len(left_out) == 9
    assert all(a["reason"] for a in left_out)


def test_a_withheld_initiative_names_the_withhold():
    facts = _facts(initiatives=[_unstarted("a")], quarantines=[QUARANTINE])
    assert _unlaunched(facts) == {"a": "quarantined"}


def test_a_budget_skip_names_the_budget_and_when_it_clears():
    facts = _facts(initiatives=[_unstarted("a")], launch_history={"a": {"launches": RECENT}}, max_launches_per_hour=2)
    assert _unlaunched(facts) == {
        "a": "launch budget: 2 launches in the last hour (max_launches_per_hour=2), clears at 2026-10-07T12:40:00+00:00"
    }


def test_a_steer_deferral_names_the_initiative_it_waits_on():
    facts = _facts(
        initiatives=[_surfaced("x", ["src/main.rs"])],
        running=[{"id": "y", "repo": "r", "surfaces": ["src/main.rs"]}],
    )
    assert _unlaunched(facts) == {"x": "steer hold: waiting on y"}


def _schedule_inputs(ids: str, mode: str, hosts: list[dict]) -> dict:
    phase = {"id": "p1", "status": "ready", "parents": [], "size": "S", "mix": "plain", "surfaces": []}
    return {
        "initiatives": [{"id": i, "priority": n, "repo": "r", "phases": [phase]} for n, i in enumerate(ids)],
        "records": [],
        "hosts": hosts,
        "mode": mode,
        "load": {},
        "target": None,
    }


CHAIR = {"name": "chair", "lanes": ["l1"], "local": True, "repos": []}
HOST_A = {"name": "host-a", "lanes": ["l1"], "local": False, "repos": []}


def test_orchestration_mode_names_the_chair_machine_for_an_initiative_the_schedule_placed_there():
    dispatch = {"max_in_flight": 5, "live_runs": 0, "hosts": [], "local_lanes": "decompose"}
    facts = _facts(
        initiatives=[_unstarted("a")], dispatch=dispatch, schedule_inputs=_schedule_inputs("a", "building", [CHAIR])
    )
    assert _unlaunched(facts) == {"a": "orchestration mode: it would build on the chair machine"}


def test_orchestration_mode_reason_is_not_given_to_a_full_lane_host_or_a_later_placement():
    dispatch = {
        "max_in_flight": 5,
        "live_runs": 1,
        "hosts": [{"name": "host-a", "live_runs": 1, "capacity": 1}],
        "local_lanes": "decompose",
    }
    facts = _facts(
        initiatives=[_unstarted("a"), _unstarted("b")],
        dispatch=dispatch,
        schedule_inputs=_schedule_inputs("ab", "orchestrating", [CHAIR, HOST_A]),
    )
    # One lane: the initiative placed now finds its host full, the other waits behind it. Neither builds on the chair machine.
    assert sorted(_unlaunched(facts).values()) == ["no lane free on an eligible host", "the schedule placed it later"]


def test_a_launch_cap_of_zero_names_the_pacing():
    limits = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 0, "go_degraded": False}
    assert _unlaunched(_facts(initiatives=[_unstarted("a")], limits=limits)) == {"a": "paced against the weekly target"}


def test_a_full_machine_names_the_missing_lane():
    facts = _facts(initiatives=[_unstarted("a")], dispatch={"max_in_flight": 1, "live_runs": 1, "hosts": []})
    assert _unlaunched(facts) == {"a": "no lane free on an eligible host"}


def test_free_lanes_with_ready_initiatives_and_nothing_launched_give_one_action_per_initiative():
    quarantines = [{**QUARANTINE, "task_id": f"{n}-t", "initiative": n} for n in "abc"]
    facts = _facts(initiatives=[_unstarted(n) for n in "abc"], quarantines=quarantines)
    actions = plan_tick(facts, NOW)
    assert not [a for a in actions if a["kind"] == "launch_epic"]
    assert _unlaunched(facts) == {"a": "quarantined", "b": "quarantined", "c": "quarantined"}


def test_a_hard_stop_gives_every_ready_initiative_a_line():
    limits = {"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}
    facts = _facts(initiatives=[_unstarted("a"), _unstarted("b")], limits=limits)
    assert _unlaunched(facts) == {
        "a": "hard stop: nothing launches until the stop lifts",
        "b": "hard stop: nothing launches until the stop lifts",
    }


def test_without_now_no_unlaunched_action_is_planned():
    actions = plan_tick(_facts(initiatives=[_unstarted("a")], quarantines=[QUARANTINE]))
    assert actions and all(a["kind"] != "unlaunched" for a in actions)


def test_an_initiative_with_a_steer_head_stuck_line_gets_no_second_line():
    stuck = [{"kind": "needs_chair", "initiative": "a", "cause": "steer_head_stuck", "reason": "no host"}]
    facts = _facts(initiatives=[_unstarted("a"), _unstarted("b")])
    actions = _unlaunched_actions(facts, [], {"quarantined": frozenset("ab")}, {}, 0, False, 5, NOW, stuck)
    assert [a["initiative"] for a in actions] == ["b"]


def test_a_lent_lease_plans_no_unlaunched_action_where_the_holder_does():
    facts = {"initiatives": [_unstarted("a")], "quarantines": [QUARANTINE]}
    holder = plan_tick_held(_facts(**facts), NOW).actions
    lent = plan_tick_held(_facts(lease=_lease(lent=True), **facts), NOW).actions
    assert [a["kind"] for a in holder if a["kind"] == "unlaunched"] == ["unlaunched"]
    assert "unlaunched" not in [a["kind"] for a in lent]


def _steward_facts(epic: dict, **initiative) -> dict:
    return _facts(initiatives=[{**_initiative("a"), **initiative}], run_exited={"a": True}, steward=_steward(epic))


def test_a_steward_handled_initiative_with_a_dag_problem_reports_the_invalid_dag():
    facts = _steward_facts(_epic(("a-t", "ready"), exited=False), dag_problem="b needs x")
    assert _unlaunched(facts) == {"a": "invalid DAG: b needs x"}


def test_a_steward_handled_initiative_with_a_planned_merge_is_held_by_the_steward():
    # A launch cap of 0 drops the relaunch tagged after_steward, so the initiative is left out beside its merge.
    paced = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 0, "go_degraded": False}
    facts = {**_steward_facts(_epic(("a-t", "approved"))), "limits": paced}
    actions = plan_tick(facts, NOW)
    assert [a["kind"] for a in actions if a["kind"] == "merge_level"] == ["merge_level"]
    assert _unlaunched(facts) == {"a": "the steward holds it"}


def test_a_steward_handled_initiative_with_nothing_planned_does_not_claim_the_steward_holds_it():
    facts = _steward_facts(_epic(("a-t", "ready"), exited=False))
    assert _unlaunched(facts) == {"a": "the steward planned nothing for it this tick"}


UNFETCHED = {"max_in_flight": 5, "live_runs": 0, "hosts": [{"name": "host-a", "live_runs": 0, "capacity": 1}]}


def test_a_decompose_only_unfetched_initiative_launches_in_the_same_tick_and_gets_a_fetch_exit():
    facts = _facts(
        initiatives=[_unstarted("a")],
        dispatch=UNFETCHED,
        remote_unfetched={"a": "run-1"},
        remote_unfetched_decompose=["a"],
    )
    actions = plan_tick(facts, NOW)
    assert [a["initiative"] for a in actions if a["kind"] == "launch_epic"] == ["a"]
    assert [a["initiative"] for a in actions if a["kind"] == "fetch_exit"] == ["a"]


def test_a_build_unfetched_initiative_is_still_withheld():
    facts = _facts(initiatives=[_unstarted("a")], dispatch=UNFETCHED, remote_unfetched={"a": "run-1"})
    actions = plan_tick(facts, NOW)
    assert [a for a in actions if a["kind"] == "launch_epic"] == []
    assert _unlaunched(facts) == {"a": "its branches are not fetched"}
