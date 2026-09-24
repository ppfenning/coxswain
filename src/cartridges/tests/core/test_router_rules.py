import pytest

from core.router_rules import (
    Hints,
    History,
    _step,
    challenger_drop,
    degraded_drop,
    escalation_step,
    floor_class,
    retry_bump,
    select_class,
    select_effort,
    size_bump,
)

LADDER = ("light", "standard", "deep")
EFFORT = ("low", "medium", "high")
FLOORS = {"build": "standard", "review": "standard", "plan": "light"}
REVIEWED = frozenset({"review"})
KW = {"reviewed": REVIEWED, "diff_threshold": 400, "file_threshold": 10}


def hist(calls: int, landed: float = 1.0, role: str = "review") -> History:
    return History(role_calls={role: calls}, landed_rate=landed)


def test_rule1_floor_class_comes_from_the_mapping():
    assert floor_class("build", LADDER, FLOORS) == ("standard", "floor standard for build")


def test_rule1_a_role_without_a_floor_starts_at_the_lowest_rung():
    assert floor_class("triage", LADDER, FLOORS)[0] == "light"


def test_rule2_retry_bumps_one():
    assert retry_bump(Hints(revise_or_retry=True)) == (1, "+1 revise or retry")
    assert retry_bump(Hints()) == (0, "not a revise or retry")


def test_rule3_size_bumps_past_either_threshold_only():
    assert size_bump(Hints(diff_lines=401), 400, 10)[0] == 1
    assert size_bump(Hints(file_count=11), 400, 10)[0] == 1
    assert size_bump(Hints(diff_lines=400, file_count=10), 400, 10)[0] == 0


def test_rule4_degraded_pacing_drops_one():
    assert degraded_drop(Hints(pacing_state="go_degraded")) == (-1, "-1 pacing go_degraded")
    assert degraded_drop(Hints(pacing_state="go"))[0] == 0


def test_rule5_challenger_fires_on_every_tenth_call():
    assert [challenger_drop("review", hist(n), REVIEWED)[0] for n in (9, 10, 11)] == [0, -1, 0]


def test_rule5_challenger_skips_a_seat_that_is_not_reviewed():
    assert challenger_drop("plan", hist(10, role="plan"), REVIEWED)[0] == 0


def test_rule5_challenger_every_is_a_named_parameter():
    assert challenger_drop("review", hist(5), REVIEWED, every=5)[0] == -1


def test_zero_calls_never_trigger_the_challenger():
    assert challenger_drop("review", hist(0), REVIEWED)[0] == 0


def test_escalation_holds_at_19_calls_and_may_escalate_at_20():
    assert escalation_step("review", hist(19, 0.10))[0] == 0
    assert escalation_step("review", hist(20, 0.10))[0] == 1


def test_escalation_at_0_69_landed_but_not_at_0_70():
    assert escalation_step("review", hist(20, 0.69))[0] == 1
    assert escalation_step("review", hist(20, 0.70))[0] == 0


def test_step_stops_at_both_ends_of_the_ladder():
    assert _step(LADDER, "deep", 1) == "deep"
    assert _step(LADDER, "light", -1) == "light"
    assert _step(LADDER, "light", 1) == "standard"


def test_select_class_starts_at_the_floor():
    assert select_class("review", LADDER, FLOORS, Hints(), hist(3), **KW) == (
        "standard",
        "floor standard for review; no rule fired",
    )


def test_select_class_retry_and_degraded_and_challenger_compose():
    hints = Hints(revise_or_retry=True, pacing_state="go_degraded")
    chosen, reason = select_class("review", LADDER, FLOORS, hints, hist(10), **KW)
    assert chosen == "light"
    assert reason == "floor standard for review; +1 revise or retry; -1 pacing go_degraded; -1 challenger on call 10"


def test_select_class_escalates_on_a_low_landed_rate():
    assert select_class("review", LADDER, FLOORS, Hints(), hist(21, 0.5), **KW)[0] == "deep"


def test_floor_exemption_build_and_arbitrate_never_go_below_floor():
    degraded = Hints(pacing_state="go_degraded")
    floors = {"build": "standard", "arbitrate": "standard", "review": "standard"}
    kw = {**KW, "reviewed": frozenset({"build", "arbitrate", "review"})}
    for role in ("build", "arbitrate"):
        assert select_class(role, LADDER, floors, degraded, hist(10, role=role), **kw)[0] == "standard"
        assert select_effort(role, EFFORT, "medium", degraded, hist(10, role=role), **kw)[0] == "medium"
    assert select_class("review", LADDER, floors, degraded, hist(10), **kw)[0] == "light"
    assert select_effort("review", EFFORT, "medium", degraded, hist(10), **kw)[0] == "low"


def test_select_effort_steps_up_on_retry_and_size_and_stops_at_the_top():
    hints = Hints(revise_or_retry=True, diff_lines=999)
    assert select_effort("plan", EFFORT, "medium", hints, hist(1, role="plan"), **KW)[0] == "high"
    assert select_effort("plan", EFFORT, "high", hints, hist(1, role="plan"), **KW)[0] == "high"


def test_inputs_are_frozen():
    with pytest.raises(AttributeError):
        Hints().diff_lines = 1
