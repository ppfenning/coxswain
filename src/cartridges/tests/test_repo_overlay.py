from __future__ import annotations

from pathlib import Path

import yaml

from core.cartridge import load, overlay_errors
from core.skills import index_from_roots

PACKAGE = Path(__file__).resolve().parent.parent
OVERLAY = Path(__file__).resolve().parents[3] / ".agent" / "cartridge.yaml"


def _overlay() -> dict:
    assert OVERLAY.is_file(), f"repository overlay missing at {OVERLAY}"
    return yaml.safe_load(OVERLAY.read_text())


def test_repo_overlay_has_no_errors() -> None:
    assert overlay_errors(_overlay()) == []


def test_repo_overlay_sets_land_checks_ci_and_leaves_land_at_to_the_team() -> None:
    # The land_at level stop-gap is gone: ticket granularity unblocks dependent phases on a stored merge.
    assert _overlay() == {"landing_areas": {"land_checks": "ci"}}


def test_local_team_without_overlay_leaves_land_checks_unset() -> None:
    resolved = load("local", PACKAGE / "cartridges", skill_index=index_from_roots([PACKAGE / "skills-plugins"]))
    assert "land_checks" not in resolved.get("landing_areas", {})
