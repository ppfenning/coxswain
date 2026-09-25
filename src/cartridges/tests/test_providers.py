"""providers/claude-code.yaml, resolved the way the runner reads it: a bare
YAML load, since nothing in this repo parses the provider profile today."""

from __future__ import annotations

import re
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
    assert PROFILE["defaults"]["sweep_plan"] == "judge"
    assert PROFILE["role_budget_usd"]["sweep_plan"] == 1.50


def test_sweep_build_carries_the_standard_build_tool_grant_and_tier() -> None:
    assert set(PROFILE["tools"]["sweep_build"]) == set(PROFILE["tools"]["build"])
    assert PROFILE["defaults"]["sweep_build"] == "reason"
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


def test_local_oss_defaults_are_classes_and_overrides_are_declared_tiers() -> None:
    assert set(LOCAL_PROFILE["defaults"].values()) <= {"extract", "reason", "judge", "frontier"}
    assert set(LOCAL_PROFILE["tier_overrides"].values()) <= set(LOCAL_PROFILE["tiers"])


def test_local_oss_role_budgets_match_claude_code_for_the_shared_roles() -> None:
    for role in ("review_charter", "review_adversary", "triage", "build"):
        assert LOCAL_PROFILE["role_budget_usd"][role] == PROFILE["role_budget_usd"][role]


def test_local_oss_defaults_are_copied_verbatim_from_claude_code() -> None:
    assert LOCAL_PROFILE["defaults"] == PROFILE["defaults"]


ANTHROPIC_PROFILE = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "providers" / "anthropic-default.yaml").read_text()
)

GRAPH_ROLES = (
    "decompose",
    "dispatch",
    "handoff",
    "reconcile",
    "retro",
    "scope_epic",
    "triage",
    "validate_chunk",
    "validate_phase",
)


def test_claude_code_defaults_record_the_graph_roles_at_todays_classes() -> None:
    defaults = PROFILE["defaults"]
    assert set(GRAPH_ROLES) <= set(defaults)
    assert defaults["validate_phase"] == "judge"
    assert defaults["retro"] == "judge"
    assert defaults["triage"] == "judge"


def test_anthropic_default_defaults_record_the_graph_roles_at_todays_classes() -> None:
    defaults = ANTHROPIC_PROFILE["defaults"]
    assert set(GRAPH_ROLES) <= set(defaults)
    assert defaults["validate_phase"] == "judge"
    assert defaults["retro"] == "judge"
    assert defaults["triage"] == "judge"


def test_local_oss_defaults_record_the_graph_roles_at_todays_classes() -> None:
    defaults = LOCAL_PROFILE["defaults"]
    assert set(GRAPH_ROLES) <= set(defaults)
    assert defaults["validate_phase"] == "judge"
    assert defaults["retro"] == "judge"
    assert defaults["triage"] == "judge"


SYSTEM_ONE = {
    "backend": "knn-local",
    "model": "all-MiniLM-L6-v2",
    "embedding_model": "all-MiniLM-L6-v2",
    "examples": "~/.local/state/coxswain/system_one/examples.jsonl",
    "k": 5,
    "device": "cpu",
    "roles": {
        "handoff": {"mode": "shadow", "threshold": 0.9},
        "review_charter": {"mode": "shadow", "threshold": 0.9},
    },
}


def test_claude_code_system_one_is_the_local_shadow_default() -> None:
    assert PROFILE["system_one"] == SYSTEM_ONE


def test_local_oss_carries_the_same_system_one_block() -> None:
    assert LOCAL_PROFILE["system_one"] == PROFILE["system_one"]


def test_anthropic_default_has_no_system_one_key() -> None:
    assert "system_one" not in ANTHROPIC_PROFILE


def test_system_one_block_satisfies_the_graphs_parser_rules() -> None:
    block = PROFILE["system_one"]
    assert {"backend", "model", "roles"} <= set(block)
    model = block["model"]
    assert isinstance(model, str) and model and not model.endswith("latest")
    assert re.search(r"\d+(\.\d+)*", model)
    for role in block["roles"].values():
        assert role["mode"] in {"off", "shadow", "on"}
        threshold = role["threshold"]
        assert isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
        assert 0 <= threshold <= 1
