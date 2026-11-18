"""resolve_cartridge folds the overlay it is given into the cartridge core's load resolves. No mocks."""

from __future__ import annotations

from pathlib import Path

from harness.cli import _read_overlay
from harness.resolve import overlay_path, resolve_cartridge

_CARTRIDGES_ROOT = Path(__file__).resolve().parents[2] / "cartridges"
CARTRIDGES = _CARTRIDGES_ROOT / "cartridges"
SKILLS = _CARTRIDGES_ROOT / "skills-plugins"


def test_overlay_path_joins_the_repo_to_the_fixed_location() -> None:
    assert overlay_path("/repo") == "/repo/.agent/cartridge.yaml"


def test_an_overlay_is_folded_into_the_shipped_cartridge_it_resolves() -> None:
    """The real `load` on the shipped example team: the overlay's value wins, and without one the team's own stands."""
    with_overlay, _ = resolve_cartridge(
        "example-team", cartridges_dir=CARTRIDGES, skills_root=[SKILLS], unverified_skills=True,
        overlay={"description": "project layer"},
    )
    plain, _ = resolve_cartridge("example-team", cartridges_dir=CARTRIDGES, skills_root=[SKILLS], unverified_skills=True)
    assert with_overlay["description"] == "project layer"
    assert plain["description"] not in ("project layer", None)


def test_read_overlay_parses_the_file_when_the_repo_has_one(tmp_path) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "cartridge.yaml").write_text("description: project layer\n", encoding="utf-8")

    assert _read_overlay(str(tmp_path)) == {"description": "project layer"}


def test_read_overlay_is_none_when_the_repo_has_no_overlay_file(tmp_path) -> None:
    assert _read_overlay(str(tmp_path)) is None
