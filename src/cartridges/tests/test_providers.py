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
