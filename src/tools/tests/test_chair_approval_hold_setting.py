from dataclasses import replace
from datetime import UTC, datetime

from test_chair_facts import _deps

from agent_tools import cli, route
from agent_tools.chair_facts import gather_facts, resolve_approval_hold_ticks

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _hold_ticks_in_facts(tmp_path, profile: dict) -> int:
    wired = cli._chair_run_deps(
        tmp_path / "runs", profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files"
    )
    deps = replace(_deps(), approval_hold_ticks=wired.facts_deps.approval_hold_ticks)
    return gather_facts(deps, NOW)["approval_hold_ticks"]


def test_the_default_resolves_to_six(tmp_path) -> None:
    assert resolve_approval_hold_ticks(None) == 6
    assert _hold_ticks_in_facts(tmp_path, {}) == 6


def test_a_profile_value_of_three_reaches_the_facts_builder_as_three(tmp_path) -> None:
    assert _hold_ticks_in_facts(tmp_path, {"chair": {"approval_hold_ticks": 3}}) == 3


def test_a_non_numeric_profile_value_falls_back_to_six(tmp_path) -> None:
    assert _hold_ticks_in_facts(tmp_path, {"chair": {"approval_hold_ticks": "soon"}}) == 6


def test_the_route_lint_accepts_the_profile_key() -> None:
    assert "approval_hold_ticks" in route._CHAIR_KEYS
    assert route.parse_profile("chair:\n  approval_hold_ticks: 3\n")["chair"] == {"approval_hold_ticks": 3.0}
