"""providers/claude-code.yaml, resolved the way the runner reads it: a bare
YAML load, since nothing in this repo parses the provider profile today."""

from __future__ import annotations

from pathlib import Path

import yaml

PROFILE = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "providers" / "claude-code.yaml").read_text()
)


def test_the_profile_parses() -> None:
    assert PROFILE["profile"] == "claude-code"


def test_style_pass_carries_write_tools_and_no_bash() -> None:
    assert set(PROFILE["tools"]["style_pass"]) == {"Read", "Write", "Edit", "Grep", "Glob"}


def test_style_pass_role_budget_exceeds_the_standard_tier() -> None:
    assert PROFILE["role_budget_usd"]["style_pass"] > PROFILE["budget_usd"]["standard"]


def test_sweep_plan_carries_read_only_tools_and_deep_tier() -> None:
    assert set(PROFILE["tools"]["sweep_plan"]) == {"Read", "Grep", "Glob"}
    assert PROFILE["defaults"]["sweep_plan"] == "deep"
    assert PROFILE["role_budget_usd"]["sweep_plan"] == 1.50


def test_sweep_build_carries_the_standard_build_tool_grant_and_tier() -> None:
    assert set(PROFILE["tools"]["sweep_build"]) == set(PROFILE["tools"]["build"])
    assert PROFILE["defaults"]["sweep_build"] == "standard"
    assert PROFILE["role_budget_usd"]["sweep_build"] == 2.00


def test_triage_role_budget_is_eighty_cents() -> None:
    assert PROFILE["role_budget_usd"]["triage"] == 0.80


LOCAL_PROFILE = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "providers" / "local-oss.yaml").read_text()
)


def test_the_local_oss_profile_parses() -> None:
    assert LOCAL_PROFILE["profile"] == "local-oss"


def test_local_oss_capabilities_are_false_until_measured() -> None:
    caps = LOCAL_PROFILE["capabilities"]
    assert set(caps) == {"structured_output", "tool_use", "resume", "streaming", "max_context"}
    assert caps["structured_output"] is False
    assert caps["tool_use"] is False
    assert caps["resume"] is False
    assert caps["streaming"] is False


def test_local_oss_every_routed_role_resolves_to_a_declared_tier() -> None:
    tiers = set(LOCAL_PROFILE["tiers"])
    roles = set(LOCAL_PROFILE["defaults"].values()) | set(LOCAL_PROFILE["tier_overrides"].values())
    assert roles <= tiers


def test_local_oss_role_budgets_match_claude_code_for_the_shared_roles() -> None:
    for role in ("review_charter", "review_adversary", "triage", "build"):
        assert LOCAL_PROFILE["role_budget_usd"][role] == PROFILE["role_budget_usd"][role]


def test_local_oss_defaults_are_copied_verbatim_from_claude_code() -> None:
    assert LOCAL_PROFILE["defaults"] == PROFILE["defaults"]
