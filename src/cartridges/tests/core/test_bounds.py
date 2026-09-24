import dataclasses

import pytest

from core.bounds import ChairBounds, from_policy, load_bounds

POLICY = {
    "pacing": {
        "tier_ladder": ["standard", "cheap"],
        "effort_ladder": ["high", "low"],
        "effort_ceiling": "low",
        "node_budget_cap_usd": 0.5,
        "run_budget_cap_usd": 2.0,
        "pacing_state": "go_degraded",
    }
}


def test_policy_alone_fills_every_field_and_defaults_nothing():
    assert from_policy(POLICY, {}) == ChairBounds("standard", "low", 0.5, 2.0, "go_degraded", ())


def test_a_flag_overrides_the_policy_value():
    flags = {"tier_ceiling": "cheap", "effort_ceiling": "high", "run_budget_cap_usd": 1.0}
    assert from_policy(POLICY, flags) == ChairBounds("cheap", "high", 0.5, 1.0, "go_degraded", ())


def test_missing_values_use_the_named_defaults_and_say_so():
    assert from_policy({}, {}) == ChairBounds(
        "deep",
        "high",
        None,
        None,
        "normal",
        ("class_ceiling", "effort_ceiling", "node_budget_cap_usd", "run_budget_cap_usd", "pacing_state"),
    )


def test_an_unknown_class_is_an_error_value():
    assert from_policy({}, {"tier_ceiling": "huge"}) == [
        "class_ceiling: unknown value 'huge', expected one of ['deep', 'standard', 'cheap']"
    ]


def test_an_unknown_effort_is_an_error_value():
    assert from_policy({}, {"effort_ceiling": "max"}) == [
        "effort_ceiling: unknown value 'max', expected one of ['high', 'low']"
    ]


def test_a_negative_cap_is_an_error_value_and_every_error_is_reported():
    assert from_policy({}, {"node_budget_cap_usd": -1, "run_budget_cap_usd": -0.5}) == [
        "node_budget_cap_usd: must not be negative, got -1",
        "run_budget_cap_usd: must not be negative, got -0.5",
    ]


def test_a_zero_cap_is_valid():
    assert from_policy({}, {"node_budget_cap_usd": 0}).node_budget_cap_usd == 0


def test_the_record_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        from_policy({}, {}).class_ceiling = "cheap"  # type: ignore[misc]


def test_the_edge_reads_the_yaml_policy_and_applies_flags(tmp_path):
    path = tmp_path / "cartridge.yaml"
    path.write_text("policy:\n  pacing:\n    effort_ceiling: low\n    run_budget_cap_usd: 3.0\n")
    assert load_bounds(path, {"tier_ceiling": "cheap"}) == ChairBounds(
        "cheap", "low", None, 3.0, "normal", ("node_budget_cap_usd", "pacing_state")
    )
