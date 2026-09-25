import dataclasses

import pytest

from core.bounds import CAPABILITY_CLASSES, ChairBounds, from_policy, load_bounds

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
    assert from_policy(POLICY, {}) == ChairBounds("reason", "low", 0.5, 2.0, "go_degraded", ())


def test_a_flag_overrides_the_policy_value():
    flags = {"tier_ceiling": "cheap", "effort_ceiling": "high", "run_budget_cap_usd": 1.0}
    assert from_policy(POLICY, flags) == ChairBounds("extract", "high", 0.5, 1.0, "go_degraded", ())


def test_missing_values_use_the_named_defaults_and_say_so():
    assert from_policy({}, {}) == ChairBounds(
        "frontier",
        "high",
        None,
        None,
        "go",
        ("class_ceiling", "effort_ceiling", "node_budget_cap_usd", "run_budget_cap_usd", "pacing_state"),
    )


def test_an_unknown_class_is_an_error_value():
    assert from_policy({}, {"tier_ceiling": "huge"}) == [
        "class_ceiling: unknown value 'huge', expected one of "
        "['extract', 'reason', 'judge', 'frontier', 'deep', 'standard', 'cheap']"
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


def test_from_policy_accepts_stop():
    assert from_policy({}, {"pacing_state": "stop"}).pacing_state == "stop"


def test_the_record_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        from_policy({}, {}).class_ceiling = "extract"  # type: ignore[misc]


def test_the_edge_reads_the_yaml_policy_and_applies_flags(tmp_path):
    path = tmp_path / "cartridge.yaml"
    path.write_text("policy:\n  pacing:\n    effort_ceiling: low\n    run_budget_cap_usd: 3.0\n")
    assert load_bounds(path, {"tier_ceiling": "cheap"}) == ChairBounds(
        "extract", "low", None, 3.0, "go", ("node_budget_cap_usd", "pacing_state")
    )


def test_the_classes_are_ordered_lowest_rank_first():
    assert CAPABILITY_CLASSES == ("extract", "reason", "judge", "frontier")


@pytest.mark.parametrize("name", ["extract", "reason", "judge", "frontier"])
def test_a_class_name_is_accepted_as_is_from_a_flag_and_from_policy(name):
    assert from_policy({}, {"tier_ceiling": name}).class_ceiling == name
    assert from_policy({"pacing": {"class_ceiling": name}}, {}).class_ceiling == name


LEGACY = [("deep", "frontier"), ("standard", "reason"), ("cheap", "extract")]


@pytest.mark.parametrize("legacy, expected", LEGACY)
def test_a_legacy_flag_is_translated(legacy, expected):
    assert from_policy({}, {"tier_ceiling": legacy}).class_ceiling == expected


@pytest.mark.parametrize("legacy, expected", LEGACY)
def test_a_legacy_class_key_is_translated(legacy, expected):
    assert from_policy({"pacing": {"class_ceiling": legacy}}, {}).class_ceiling == expected


@pytest.mark.parametrize("legacy, expected", LEGACY)
def test_a_legacy_ladder_head_is_translated(legacy, expected):
    assert from_policy({"pacing": {"tier_ladder": [legacy]}}, {}).class_ceiling == expected


def test_a_ladder_head_outside_the_ceiling_table_is_an_error_value():
    assert from_policy({"pacing": {"tier_ladder": ["premium", "standard"]}}, {}) == [
        "class_ceiling: unknown value 'premium', expected one of "
        "['extract', 'reason', 'judge', 'frontier', 'deep', 'standard', 'cheap']"
    ]


def test_a_class_key_wins_over_the_ladder_head():
    policy = {"pacing": {"class_ceiling": "judge", "tier_ladder": ["cheap"]}}
    assert from_policy(policy, {}).class_ceiling == "judge"


def test_a_legacy_flag_overrides_a_class_policy():
    assert from_policy({"pacing": {"class_ceiling": "judge"}}, {"tier_ceiling": "cheap"}).class_ceiling == "extract"


def test_a_class_flag_overrides_a_legacy_policy():
    assert from_policy(POLICY, {"tier_ceiling": "judge"}).class_ceiling == "judge"


def test_a_null_class_key_falls_back_to_the_default_and_says_so():
    bounds = from_policy({"pacing": {"class_ceiling": None}}, {})
    assert (bounds.class_ceiling, "class_ceiling" in bounds.defaulted) == ("frontier", True)


def test_an_unknown_policy_class_is_an_error_value_naming_the_typed_value():
    assert from_policy({"pacing": {"class_ceiling": "huge"}}, {}) == [
        "class_ceiling: unknown value 'huge', expected one of "
        "['extract', 'reason', 'judge', 'frontier', 'deep', 'standard', 'cheap']"
    ]
