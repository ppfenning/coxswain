import dataclasses

import pytest

from core.bounds import ChairBounds, from_policy
from core.catalog import Catalog, ModelEntry, Price
from core.router import Decision, decide
from core.router_rules import Hints, History


def model(id: str, classes: tuple[str, ...], effort: tuple[str, ...], cost: float) -> ModelEntry:
    return ModelEntry(id, (), "p", Price(cost, cost * 5, cost, cost), effort, classes, None)


# Prices summed input plus output: frontier 90, judge 30, reason 18, extract 6.
CATALOG = Catalog(
    (
        model("m-frontier", ("frontier",), ("low", "medium", "high", "xhigh"), 15.0),
        model("m-judge", ("judge",), ("low", "medium", "high"), 5.0),
        model("m-reason", ("reason",), ("low", "medium", "high"), 3.0),
        model("m-extract", ("extract",), ("low", "medium"), 1.0),
    )
)
OPEN = ChairBounds("frontier", "xhigh", None, None, "go", ())
RETRY = Hints(revise_or_retry=True)
NO_HISTORY = History(role_calls={})
NO_HINTS = Hints()
ON_EXTRACT = (("m-extract", 2.0),)


def run(role="plan", hints=NO_HINTS, history=NO_HISTORY, bounds=OPEN, catalog=CATALOG, observed=(), spend=0.0):
    return decide(role, hints, history, bounds, catalog, observed_costs=observed, spend_usd=spend)


def test_no_rule_fired_gives_the_floor_class_base_effort_and_floor_budget():
    assert run() == Decision(
        "m-extract",
        "medium",
        0.25,
        (
            "class: no usable floor for plan, lowest rung extract",
            "effort: base medium",
            "budget: no observed turn costs; using floor $0.25",
        ),
        (),
    )


def test_a_retry_surfaces_for_class_and_effort():
    result = run(hints=RETRY)
    assert (result.model, result.effort) == ("m-reason", "high")
    assert {"class: +1 revise or retry", "effort: +1 revise or retry"} <= set(result.reasons)


def test_size_surfaces_when_past_a_threshold():
    assert "class: +1 size: 500 diff lines, 0 files" in run(hints=Hints(diff_lines=500)).reasons


def test_degraded_pacing_in_the_hints_surfaces_and_lowers_effort_without_naming_pacing():
    result = run(hints=Hints(pacing_state="go_degraded"))
    assert "effort: -1 pacing go_degraded" in result.reasons
    assert (result.effort, result.clipped_by) == ("low", ())


def test_the_challenger_surfaces_on_every_tenth_call_of_a_reviewed_seat():
    result = run(role="review", history=History(role_calls={"review": 10}))
    assert "class: -1 challenger on call 10" in result.reasons


def test_escalation_surfaces_when_the_landed_rate_is_low_after_twenty_calls():
    result = run(history=History(role_calls={"plan": 20}, landed_rate=0.5))
    assert "class: +1 landed rate 0.5 under 0.7" in result.reasons
    assert result.model == "m-reason"


def test_build_never_drops_below_floor_under_degraded_pacing_or_a_challenge():
    result = run(role="build", hints=Hints(pacing_state="go_degraded"), history=History(role_calls={"build": 10}))
    assert result.model == "m-reason"
    assert "class: floor reason for build" in result.reasons
    assert not any(r.startswith("class: -1") for r in result.reasons)


def test_spend_and_observed_costs_are_required_so_a_cap_cannot_be_left_off_silently():
    with pytest.raises(TypeError):
        decide("plan", NO_HINTS, NO_HISTORY, OPEN, CATALOG)  # type: ignore[call-arg]


def test_the_budget_rule_gets_the_picked_models_price_ratio_against_the_cheapest_model():
    result = run(hints=RETRY, observed=(("m-extract", 0.2),))
    assert result.budget_usd == pytest.approx(0.9)
    assert "budget: median $0.20 x margin 1.5 x ratio 3.0 = $0.90" in result.reasons


def test_an_expensive_models_observation_does_not_inflate_a_cheaper_models_budget():
    assert run(hints=RETRY, observed=(("m-frontier", 3.0),)).budget_usd == pytest.approx(0.9)


def test_an_observation_for_a_model_not_in_the_catalog_is_ignored_and_named():
    result = run(observed=(("m-retired", 5.0),))
    assert result.budget_usd == 0.25
    assert "ignored observed turn cost for unknown model m-retired" in result.reasons


def test_an_unsupported_effort_falls_back_to_the_nearest_supported_and_says_so():
    bounds = ChairBounds("extract", "xhigh", None, None, "go", ())
    result = run(hints=RETRY, bounds=bounds)
    assert (result.model, result.effort) == ("m-extract", "medium")
    assert "effort high unsupported for class extract; using medium" in result.reasons


def test_the_class_ceiling_repicks_the_model_and_names_the_bound():
    bounds = ChairBounds("reason", "xhigh", None, None, "go", ())
    result = run(hints=Hints(diff_lines=500), history=History(role_calls={"plan": 20}, landed_rate=0.1), bounds=bounds)
    assert (result.model, result.clipped_by) == ("m-reason", ("class_ceiling",))
    assert "class judge clipped to ceiling reason" in result.reasons


def test_a_ceiling_below_floor_forces_build_down_and_says_so():
    result = run(role="build", bounds=ChairBounds("extract", "xhigh", None, None, "go", ()))
    assert (result.model, result.clipped_by) == ("m-extract", ("class_ceiling",))
    assert "build forced below floor by class_ceiling" in result.reasons


def test_the_effort_ceiling_names_the_bound():
    result = run(hints=RETRY, bounds=ChairBounds("frontier", "medium", None, None, "go", ()))
    assert (result.model, result.effort, result.clipped_by) == ("m-reason", "medium", ("effort_ceiling",))


def test_the_node_cap_names_the_bound():
    result = run(observed=ON_EXTRACT, bounds=ChairBounds("frontier", "xhigh", 1.0, None, "go", ()))
    assert (result.budget_usd, result.clipped_by) == (1.0, ("node_budget_cap",))
    assert "budget $3.00 clipped to $1.00 by node_budget_cap" in result.reasons


def test_spend_already_recorded_makes_the_run_cap_bind_and_name_itself():
    bounds = ChairBounds("frontier", "xhigh", 1.0, 2.0, "go", ())
    result = run(observed=ON_EXTRACT, bounds=bounds, spend=1.5)
    assert (result.budget_usd, result.clipped_by) == (0.5, ("run_budget_cap",))


def test_an_overspent_run_returns_an_error_value_not_a_zero_budget():
    bounds = ChairBounds("frontier", "xhigh", None, 2.0, "go", ())
    assert run(bounds=bounds, spend=3.0) == ["run budget exhausted: spent $3.00 of $2.00"]


def test_a_run_spent_exactly_to_its_cap_is_exhausted():
    bounds = ChairBounds("frontier", "xhigh", None, 2.0, "go", ())
    assert run(bounds=bounds, spend=2.0) == ["run budget exhausted: spent $2.00 of $2.00"]


def test_a_budget_under_every_cap_is_not_clipped():
    assert run(bounds=ChairBounds("frontier", "xhigh", 5.0, 5.0, "go", ()), spend=1.0).clipped_by == ()


def test_pacing_names_the_bound_when_the_chair_state_changed_the_outcome():
    result = run(hints=RETRY, bounds=ChairBounds("frontier", "xhigh", None, None, "go_degraded", ()))
    assert (result.model, result.effort, result.clipped_by) == ("m-extract", "medium", ("pacing",))
    assert "class: -1 pacing go_degraded" in result.reasons


def test_pacing_is_not_named_when_the_hints_already_carried_it():
    hints = Hints(pacing_state="go_degraded")
    assert run(hints=hints, bounds=ChairBounds("frontier", "xhigh", None, None, "go_degraded", ())).clipped_by == ()


def test_pacing_stop_in_the_bounds_returns_an_error_value_not_a_model():
    assert run(bounds=ChairBounds("frontier", "xhigh", None, None, "stop", ())) == [
        "pacing state is stop: no model to route"
    ]


def test_pacing_stop_in_the_hints_returns_an_error_value_not_a_model():
    assert run(hints=Hints(pacing_state="stop")) == ["pacing state is stop: no model to route"]


def test_two_bounds_clipping_at_once_are_both_named_in_order():
    bounds = ChairBounds("extract", "xhigh", 0.5, None, "go", ())
    result = run(hints=RETRY, bounds=bounds, observed=ON_EXTRACT)
    assert (result.model, result.effort, result.budget_usd) == ("m-extract", "medium", 0.5)
    assert result.clipped_by == ("class_ceiling", "node_budget_cap")


def test_pacing_and_the_effort_ceiling_clip_together_and_are_both_named():
    bounds = ChairBounds("frontier", "low", None, None, "go_degraded", ())
    result = run(hints=RETRY, bounds=bounds)
    assert (result.model, result.effort, result.clipped_by) == ("m-extract", "low", ("pacing", "effort_ceiling"))


def test_pacing_and_the_run_cap_clip_together_and_are_both_named():
    bounds = ChairBounds("frontier", "xhigh", None, 1.0, "go_degraded", ())
    result = run(hints=RETRY, bounds=bounds, observed=ON_EXTRACT, spend=0.5)
    assert (result.budget_usd, result.clipped_by) == (0.5, ("pacing", "run_budget_cap"))


def test_pacing_is_not_named_for_a_role_that_never_goes_below_floor():
    result = run(role="build", bounds=ChairBounds("frontier", "xhigh", None, None, "go_degraded", ()))
    assert (result.model, result.effort, result.clipped_by) == ("m-reason", "medium", ())


def test_the_top_policy_tier_means_no_class_ceiling_and_adds_no_reason():
    result = run(bounds=ChairBounds("deep", "xhigh", None, None, "go", ()))
    assert result.clipped_by == ()
    assert not any("ceiling" in r for r in result.reasons)


def test_a_class_ceiling_the_router_cannot_enforce_returns_an_error_value():
    assert run(bounds=ChairBounds("cheap", "xhigh", None, None, "go", ())) == [
        "class ceiling cheap is not on the class ladder ['extract', 'reason', 'judge', 'frontier'] and cannot be enforced"
    ]


def test_an_effort_ceiling_off_the_ladder_returns_an_error_value():
    assert run(bounds=ChairBounds("frontier", "max", None, None, "go", ())) == [
        "effort ceiling max is not on the effort ladder ['low', 'medium', 'high', 'xhigh'] and cannot be enforced"
    ]


def test_a_missing_floor_class_never_seats_build_below_floor():
    no_reason = Catalog((model("m-extract", ("extract",), ("low", "medium"), 1.0), CATALOG.entries[0]))
    result = run(role="build", catalog=no_reason)
    assert result.model == "m-frontier"
    assert "no model carries class reason; using class frontier" in result.reasons


def test_a_missing_floor_class_with_nothing_at_or_above_it_inside_the_ceiling_returns_an_error_value():
    no_reason = Catalog((model("m-extract", ("extract",), ("low", "medium"), 1.0), CATALOG.entries[0]))
    bounds = ChairBounds("judge", "xhigh", None, None, "go", ())
    assert run(role="build", catalog=no_reason, bounds=bounds) == [
        "no model carries a class between reason and ceiling judge"
    ]


def test_a_missing_arbitrate_floor_class_with_only_lower_classes_returns_an_error_value():
    lower_only = Catalog((CATALOG.entries[2], CATALOG.entries[3]))
    assert run(role="arbitrate", catalog=lower_only) == ["no model carries a class between judge and ceiling frontier"]


def test_an_empty_catalog_returns_an_error_value():
    assert run(catalog=Catalog(())) == [
        "catalog has no usable model: needs a class and an effort the router's ladders know"
    ]


def test_a_catalog_with_no_class_or_effort_on_the_ladders_returns_an_error_value():
    unknown = Catalog((model("m-x", ("summarise",), ("low",), 1.0), model("m-y", ("reason",), ("turbo",), 1.0)))
    assert run(catalog=unknown)[0].startswith("catalog has no usable model")


def test_a_missing_class_falls_back_to_the_nearest_class_inside_the_ceiling():
    no_extract = Catalog((model("m-reason", ("reason",), ("low", "medium"), 3.0),))
    result = run(catalog=no_extract)
    assert result.model == "m-reason"
    assert "no model carries class extract; using class reason" in result.reasons


def test_a_missing_class_with_nothing_inside_the_ceiling_returns_an_error_value():
    no_extract = Catalog((model("m-reason", ("reason",), ("low", "medium"), 3.0),))
    bounds = ChairBounds("extract", "xhigh", None, None, "go", ())
    assert run(catalog=no_extract, bounds=bounds) == ["no model carries a class between extract and ceiling extract"]


def test_an_effort_ceiling_no_carrier_can_meet_returns_an_error_value_instead_of_exceeding_it():
    high_only = Catalog((model("m-hi", ("extract",), ("high",), 1.0),))
    bounds = ChairBounds("frontier", "low", None, None, "go", ())
    assert run(catalog=high_only, bounds=bounds) == [
        "no model carrying class extract supports an effort at or below ceiling low"
    ]


def test_default_bounds_from_policy_route_without_a_class_clip():
    result = run(bounds=from_policy({}, {}))
    assert (result.model, result.clipped_by) == ("m-extract", ())


def test_bounds_built_by_from_policy_clip_effort():
    result = run(bounds=from_policy({"pacing": {"effort_ceiling": "low"}}, {}))
    assert (result.effort, result.clipped_by) == ("low", ("effort_ceiling",))


def test_a_legacy_tier_ceiling_set_through_from_policy_clips_the_class():
    result = run(role="build", bounds=from_policy({}, {"tier_ceiling": "cheap"}))
    assert (result.model, result.clipped_by) == ("m-extract", ("class_ceiling",))


def test_bounds_built_by_from_policy_cap_the_run_from_recorded_spend():
    bounds = from_policy({"pacing": {"node_budget_cap_usd": 0.5, "run_budget_cap_usd": 2.0}}, {})
    result = run(bounds=bounds, observed=ON_EXTRACT, spend=1.9)
    assert (result.budget_usd, result.clipped_by) == (pytest.approx(0.1), ("run_budget_cap",))


def test_the_decision_is_frozen_and_its_reasons_are_a_tuple():
    result = run()
    assert isinstance(result.reasons, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.model = "x"  # type: ignore[misc]
