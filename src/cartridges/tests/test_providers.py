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
