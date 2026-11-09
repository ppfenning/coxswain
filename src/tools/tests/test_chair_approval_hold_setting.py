from __future__ import annotations

from datetime import UTC, datetime

from agent_tools import chair_facts, cli
from agent_tools.route import _CHAIR_KEYS
from agent_tools.settings_sections.defaults import DEFAULT_KEYS

_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _facts(tmp_path, profile: dict) -> dict:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    return chair_facts.gather_facts(deps.facts_deps, _NOW)


def test_the_default_resolves_to_6_and_is_a_settings_default_row():
    assert chair_facts.resolve_approval_hold_ticks(None) == 6
    row = next(k for k in DEFAULT_KEYS if k.key == "chair.approval_hold_ticks")
    assert (row.scope, row.default) == ("profile", 6)


def test_a_profile_value_of_3_reaches_the_facts_builder_as_3(tmp_path):
    assert _facts(tmp_path, {"chair": {"approval_hold_ticks": 3}})["approval_hold_ticks"] == 3
    assert _facts(tmp_path, {})["approval_hold_ticks"] == 6


def test_a_non_numeric_profile_value_falls_back_to_6(tmp_path):
    assert _facts(tmp_path, {"chair": {"approval_hold_ticks": "soon"}})["approval_hold_ticks"] == 6


def test_the_route_chair_key_set_accepts_approval_hold_ticks():
    assert "approval_hold_ticks" in _CHAIR_KEYS
