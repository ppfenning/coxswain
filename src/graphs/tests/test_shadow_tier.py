import dataclasses

import pytest

from runner.shadow_tier import Comparison, ConfigError, ShadowConfig, compare, parse_shadow_tier, shadow_verdict


def test_an_empty_mapping_or_none_parses_to_an_empty_config():
    assert parse_shadow_tier({}) == ShadowConfig()
    assert parse_shadow_tier(None) == ShadowConfig()


def test_both_roles_with_a_tier_parse():
    got = parse_shadow_tier({"validate_chunk": "cheap", "handoff": "cheap"})
    assert got == ShadowConfig((("handoff", "cheap"), ("validate_chunk", "cheap")))


def test_a_class_name_is_accepted():
    assert parse_shadow_tier({"handoff": "extract"}) == ShadowConfig((("handoff", "extract"),))


def test_a_build_role_is_a_config_error_naming_it():
    got = parse_shadow_tier({"build": "cheap"})
    assert isinstance(got, ConfigError)
    assert got.role == "build"
    assert "build" in got.message


def test_an_unknown_role_is_a_config_error_naming_it():
    got = parse_shadow_tier({"handoff": "cheap", "plan": "cheap"})
    assert got == ConfigError(got.message, "plan")
    assert "plan" in got.message


@pytest.mark.parametrize("tier", ["", None, 3, "huge"])
def test_a_bad_tier_is_a_config_error_naming_the_role(tier):
    got = parse_shadow_tier({"handoff": tier})
    assert isinstance(got, ConfigError)
    assert got.role == "handoff"


def test_a_non_mapping_is_a_config_error():
    assert isinstance(parse_shadow_tier(["handoff"]), ConfigError)


def test_the_config_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        ShadowConfig().tiers = (("handoff", "cheap"),)


def test_handoff_reads_complete_and_ignores_satisfied():
    assert shadow_verdict("handoff", {"complete": True, "satisfied": False}) is True


def test_validate_chunk_reads_satisfied_and_ignores_complete():
    assert shadow_verdict("validate_chunk", {"complete": True, "satisfied": False}) is False


@pytest.mark.parametrize("result", [{}, {"complete": "true"}, {"complete": 1}, {"complete": None}])
def test_a_missing_or_non_boolean_key_is_none(result):
    assert shadow_verdict("handoff", result) is None


def test_an_unknown_role_has_no_verdict():
    assert shadow_verdict("build", {"complete": True}) is None


def test_equal_verdicts_agree_and_are_not_costly():
    assert compare("handoff", {"complete": True}, {"complete": True}) == Comparison(True, False)
    assert compare("handoff", {"complete": False}, {"complete": False}) == Comparison(True, False)


def test_the_shadow_passing_what_the_acting_tier_stopped_is_costly():
    assert compare("handoff", {"complete": False}, {"complete": True}) == Comparison(False, True)


def test_the_shadow_stopping_what_the_acting_tier_passed_is_not_costly():
    got = compare("validate_chunk", {"satisfied": True}, {"satisfied": False})
    assert got == Comparison(False, False)


def test_a_missing_key_on_either_side_gives_none_for_both_flags():
    assert compare("handoff", {}, {"complete": True}) == Comparison(None, None)
    assert compare("handoff", {"complete": True}, {"complete": "yes"}) == Comparison(None, None)
