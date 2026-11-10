from datetime import UTC, datetime

from agent_tools.chair_launch_budget import Launch, launch_budget
from agent_tools.chair_launch_why import LEFT_OUT, unlaunched

_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
_REFUSED = "relaunch refused by the budget or the loop rule"


def _launches(*minutes: int) -> list[Launch]:
    return [Launch(at=datetime(2026, 11, 7, 11, m, tzinfo=UTC), kind="launch_epic") for m in minutes]


def _history(*minutes: int) -> list[dict]:
    return [{"at": launch.at.isoformat(), "kind": launch.kind} for launch in _launches(*minutes)]


def _ini(id: str = "a", **extra) -> dict:
    """The InitiativeFacts shape: no host, backoff, hold or release key unless a case adds it."""
    return {"id": id, "started": False, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set(), **extra}


def _why(initiatives=None, **overrides) -> list[dict]:
    args = {
        "launched": [],
        "withheld_by": {},
        "skips": {},
        "steer_holds": [],
        "free_lanes": 2,
        "no_host": False,
        "orchestrating": False,
        "pace_cap": 3,
        "now": _NOW,
        "launch_history": [],
    }
    return unlaunched([_ini()] if initiatives is None else initiatives, **{**args, **overrides})


def _reason(**overrides) -> str:
    (action,) = _why(**overrides)
    assert action["kind"] == "unlaunched" and action["initiative"] == "a"
    return action["reason"]


def _refusal(*minutes: int, max_per_hour: int = 2) -> str:
    verdict = launch_budget(_NOW, "launch_epic", _launches(*minutes), [], "", "", max_launches_per_hour=max_per_hour)
    assert not verdict.allowed
    return verdict.reason


def test_withheld_name_is_the_reason():
    assert _reason(withheld_by={"quarantined": frozenset({"a"}), "waiting on a need": frozenset({"b"})}) == "quarantined"


def test_land_backoff_carries_its_end():
    why = _reason(initiatives=[_ini(backoff_until="2026-11-07T12:30:00+00:00")], withheld_by={"land backoff": frozenset({"a"})})
    assert why == "land backoff, ends at 2026-11-07T12:30:00+00:00"


def test_land_backoff_without_an_end_is_the_bare_name():
    assert _reason(withheld_by={"land backoff": frozenset({"a"})}) == "land backoff"


def test_launch_budget_skip_from_the_real_rule_carries_when_it_clears():
    text = _refusal(20, 50)
    assert _reason(skips={"a": text}, launch_history=_history(20, 50)) == f"{text}, clears at 2026-11-07T12:20:00+00:00"


def test_launch_budget_over_the_max_clears_when_the_count_drops_below_it():
    text = _refusal(10, 20, 50)
    assert _reason(skips={"a": text}, launch_history=_history(10, 20, 50)) == f"{text}, clears at 2026-11-07T12:20:00+00:00"


def test_budget_refused_name_with_a_confirming_skip_carries_when_it_clears():
    text = _refusal(20, 50)
    why = _reason(withheld_by={_REFUSED: frozenset({"a"})}, skips={"a": text}, launch_history=_history(20, 50))
    assert why == f"{_REFUSED}, clears at 2026-11-07T12:20:00+00:00"


def test_budget_refused_name_without_a_confirming_skip_is_bare_even_with_launches_in_the_window():
    assert _reason(withheld_by={_REFUSED: frozenset({"a"})}, launch_history=_history(20, 50)) == _REFUSED


def test_launch_budget_prefix_is_added_and_no_time_for_other_skip_text():
    assert _reason(skips={"a": "too many"}, launch_history=_history(20, 50)) == "launch budget: too many"


def test_steer_hold_names_the_other_initiative():
    holds = [{"kind": "steer_clear", "initiative": "a", "other": "b", "paths": ["x"]}]
    assert _reason(steer_holds=holds) == "steer hold: waiting on b"


def test_steer_hold_for_another_initiative_is_ignored():
    holds = [{"kind": "needs_chair", "initiative": "z", "other": "b"}]
    assert _reason(steer_holds=holds) == LEFT_OUT


def test_steer_hold_without_an_other_names_no_holder():
    holds = [{"kind": "needs_chair", "initiative": "a", "cause": "no_run"}]
    assert _reason(steer_holds=holds) == LEFT_OUT


def test_orchestration_mode_when_the_facts_say_it_builds_on_the_chair_machine():
    assert _reason(initiatives=[_ini(host="")], orchestrating=True) == "orchestration mode: it would build on the chair machine"


def test_orchestration_mode_claims_nothing_when_the_host_is_unknown_or_remote():
    assert [a["reason"] for a in _why([_ini(), _ini("b", host="h1")], orchestrating=True)] == [LEFT_OUT, LEFT_OUT]


def test_no_host_key_claims_neither_orchestration_nor_zero_lane():
    assert _reason(orchestrating=True) == LEFT_OUT
    assert _reason(orchestrating=True, free_lanes=0) == LEFT_OUT


def test_scheduled_later_is_returned_verbatim_ahead_of_orchestration():
    text = "scheduled after x/p1 on h1"
    assert _reason(initiatives=[_ini(host="", scheduled_later=text)], orchestrating=True) == text


def test_steer_hold_beats_scheduled_later():
    holds = [{"kind": "needs_chair", "initiative": "a", "other": "b"}]
    assert _reason(initiatives=[_ini(scheduled_later="the schedule placed it later")], steer_holds=holds) == "steer hold: waiting on b"


def test_dag_problem_wins_over_the_steward_hold():
    ini = _ini(dag_problem="b needs x")
    assert _reason(initiatives=[ini], withheld_by={"the steward holds it": ["a"]}) == "invalid DAG: b needs x"


def test_steward_planned_nothing_is_an_ordinary_withheld_name():
    name = "the steward planned nothing for it this tick"
    assert _reason(withheld_by={name: ["a"]}) == name


def test_pace_cap_zero_is_pacing():
    assert _reason(pace_cap=0) == "paced against the weekly target"


def test_hold_is_named():
    assert _reason(initiatives=[_ini(hold="waiting for design")]) == "on hold: waiting for design"


def test_target_release_above_current_version():
    why = _reason(initiatives=[_ini(target_release="0.48.0", current_version="0.47.0")])
    assert why == "target release 0.48.0 is above current version 0.47.0"


def test_target_release_at_or_below_current_version_is_not_a_reason():
    assert _reason(initiatives=[_ini(target_release="0.47.0", current_version="0.47.0")]) == LEFT_OUT


def test_no_host_means_no_lane_free_on_an_eligible_host():
    assert _reason(no_host=True) == "no lane free on an eligible host"


def test_zero_free_lanes_names_no_lane_only_for_an_initiative_that_needs_a_local_lane():
    assert _reason(initiatives=[_ini(host="")], free_lanes=0) == "no lane free on an eligible host"


def test_zero_free_lanes_claims_nothing_for_unknown_or_remote_host():
    assert [a["reason"] for a in _why([_ini(), _ini("b", host="h1")], free_lanes=0)] == [LEFT_OUT, LEFT_OUT]


def test_unexplained_initiative_gets_the_fallback_line():
    assert _reason() == "left out of the launch list for a reason the facts do not show"


def test_earlier_reason_wins():
    why = _reason(withheld_by={"quarantined": frozenset({"a"})}, skips={"a": "too many"}, pace_cap=0, no_host=True)
    assert why == "quarantined"


def test_launched_initiative_gets_no_line():
    assert _why(launched=["a"]) == []


def test_initiative_without_ready_tasks_gets_no_line():
    assert _why([{**_ini(), "ready_tasks": []}], no_host=True) == []


def test_arguments_are_not_mutated():
    initiatives = [_ini()]
    before = [dict(i) for i in initiatives]
    _why(initiatives, pace_cap=0)
    assert initiatives == before
