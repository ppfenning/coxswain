from datetime import UTC, datetime

from agent_tools.chair_plan import _placement_hints, _unlaunched_actions, plan_tick, plan_tick_held
from agent_tools.route_schedule import plan_from_inputs
from tests.test_chair_plan import _facts, _initiative, _lease, _unstarted
from tests.test_chair_plan_steward import _epic, _facts_for

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


ORCHESTRATING = {"max_in_flight": 5, "live_runs": 0, "hosts": [], "local_lanes": "decompose"}
HOSTS = [
    {"name": "chair", "lanes": ["l1"], "local": True, "repos": []},
    {"name": "host-a", "lanes": ["l1"], "local": False, "repos": []},
]


def _inputs(ids: list[str], hosts: list[dict], mode: str) -> dict:
    phase = {"id": "p1", "status": "ready", "parents": [], "size": "S", "mix": "plain", "surfaces": []}
    initiatives = [{"id": i, "priority": 1, "repo": "r", "phases": [phase]} for i in ids]
    return {"initiatives": initiatives, "records": [], "hosts": hosts, "mode": mode, "load": {}, "target": None}


def test_orchestration_mode_names_the_chair_machine_for_an_initiative_the_schedule_placed_on_it():
    facts = _facts(initiatives=[_unstarted("a")], dispatch=ORCHESTRATING, schedule_inputs=_inputs(["a"], HOSTS[:1], "building"))
    assert _unlaunched(facts) == {"a": "orchestration mode: it would build on the chair machine"}


def test_an_initiative_the_schedule_did_not_place_claims_nothing_about_the_chair_machine():
    assert _unlaunched(_facts(initiatives=[_unstarted("a")], dispatch=ORCHESTRATING)) == {"a": "no lane free on an eligible host"}


def test_orchestration_mode_is_not_the_reason_for_a_full_scheduled_host_or_a_later_placement():
    dispatch = {**ORCHESTRATING, "hosts": [{"name": "host-a", "live_runs": 1, "capacity": 1}]}
    facts = _facts(initiatives=[_unstarted("a"), _unstarted("b")], dispatch=dispatch, schedule_inputs=_inputs(["a", "b"], HOSTS, "orchestrating"))
    assert _unlaunched(facts) == {"a": "no lane free on an eligible host", "b": "the schedule placed it later"}


def test_a_placement_hint_names_the_scheduled_host_and_leaves_an_unplaced_initiative_without_one():
    facts = _facts(initiatives=[_unstarted("a")], schedule_inputs=_inputs(["a"], HOSTS, "building"))
    assert _placement_hints(facts, None) == {}
    assert _placement_hints(facts, plan_from_inputs(facts["schedule_inputs"], NOW)[0]) == {"a": {"host": ""}}


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


ONE_HOST = {"max_in_flight": 5, "live_runs": 0, "hosts": [{"name": "h1", "live_runs": 0, "capacity": 1}]}


def _handled_started(*tasks: tuple[str, str], merged: bool = False, dag_problem: str | None = None) -> dict:
    started = {**_initiative("a"), **({"dag_problem": dag_problem} if dag_problem else {})}
    return _facts_for(_epic(*tasks, merged=merged, exited=False), initiatives=[started])


def test_a_started_steward_handled_initiative_with_a_dag_problem_reports_the_invalid_dag_not_the_steward_hold():
    assert _unlaunched(_handled_started(("a-t", "ready"), dag_problem="b needs x")) == {"a": "invalid DAG: b needs x"}


def test_a_started_steward_handled_initiative_with_no_steward_action_reports_that_it_planned_nothing():
    assert _unlaunched(_handled_started(("a-t", "ready"))) == {"a": "the steward planned nothing for it this tick"}


def test_a_started_steward_handled_initiative_with_a_planned_gate_still_reports_the_steward_hold():
    limits = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 0, "go_degraded": False}
    facts = {**_handled_started(("a-t", "done"), merged=True), "limits": limits}
    assert [a["kind"] for a in plan_tick(facts, NOW) if a["kind"] == "run_gate"] == ["run_gate"]
    assert _unlaunched(facts) == {"a": "the steward holds it"}


def _remote(unfetched: dict[str, str], decompose: list[str]) -> dict:
    return _facts(
        initiatives=[_unstarted("a")], remote_unfetched=unfetched, remote_unfetched_decompose=decompose, dispatch=ONE_HOST
    )


def test_a_decompose_only_unfetched_initiative_launches_in_the_same_tick_and_gets_a_fetch_exit():
    actions = plan_tick(_remote({"a": "r1"}, ["a"]), NOW)
    assert [(a["kind"], a["initiative"]) for a in actions if a["kind"] in ("launch_epic", "fetch_exit")] == [
        ("fetch_exit", "a"),
        ("launch_epic", "a"),
    ]


def test_a_build_unfetched_initiative_is_still_withheld_and_gets_a_fetch_exit():
    facts = _remote({"a": "r1"}, [])
    actions = plan_tick(facts, NOW)
    assert not [a for a in actions if a["kind"] == "launch_epic"]
    assert [a["initiative"] for a in actions if a["kind"] == "fetch_exit"] == ["a"]
    assert _unlaunched(facts) == {"a": "its branches are not fetched"}
