"""The loader must refuse bad cartridges AT LOAD, and say everything that is wrong."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.skills import index_from_roots
from core.cartridge import CartridgeError, load
from tests.conftest import write_cartridge

REPO = Path(__file__).resolve().parent.parent


def test_resolves_chain_and_child_wins_on_scalars(cartridges: Path, skill_index) -> None:
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["team"] == "acme"
    # inherited from base, never restated by the team
    assert resolved["roles"]["required"] == ["plan", "build"]
    assert resolved["policy"]["graduation_n"] == 3


def test_context_concatenates_base_first(cartridges: Path, skill_index) -> None:
    resolved = load("acme", cartridges, skill_index=skill_index)
    names = [Path(p).name for p in resolved["context"]]
    assert names == ["conventions.md", "code-style.md"], "base pack must come first — order is reading order"
    assert all(Path(p).is_absolute() for p in resolved["context"])


def test_write_kinds_deep_merge_rather_than_replace(cartridges: Path, skill_index) -> None:
    resolved = load("acme", cartridges, skill_index=skill_index)
    kind = resolved["write_kinds"]["ticket_create"]
    assert kind["apply_arm"] == "plan", "team binding must land"
    assert kind["risk"] == "low" and kind["ramp"] == "deferred", "base risk/ramp must survive"


def test_sha_changes_when_a_context_pack_changes_content(cartridges: Path, skill_index) -> None:
    before = load("acme", cartridges, skill_index=skill_index)["cartridge_sha"]
    (cartridges / "acme" / "context" / "code-style.md").write_text("rewritten charter\n", encoding="utf-8")
    after = load("acme", cartridges, skill_index=skill_index)["cartridge_sha"]
    assert before != after, "editing a charter must change the hash — that is what resets autonomy"


def test_sha_is_stable_across_checkout_location(tmp_path: Path, cartridges: Path, skill_index) -> None:
    """Absolute context paths must not leak into the hash, or no streak survives a move."""
    import shutil

    here = load("acme", cartridges, skill_index=skill_index)["cartridge_sha"]
    moved = tmp_path / "elsewhere" / "cartridges"
    shutil.copytree(cartridges, moved)
    there = load("acme", moved, skill_index=skill_index)["cartridge_sha"]
    assert here == there


def test_refuses_unbound_required_role(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    del config["skills"]["build"]
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(CartridgeError, match="required role 'build' is unbound"):
        load("acme", cartridges, skill_index=skill_index)


def test_refuses_a_team_that_loosens_a_ramp(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["write_kinds"]["merge"] = {"ramp": "eligible"}  # base says never
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(CartridgeError, match="loosens merge.ramp from 'never' to 'eligible'"):
        load("acme", cartridges, skill_index=skill_index)


def test_allows_a_team_that_tightens_a_ramp(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["write_kinds"]["draft_pr_create"] = {"ramp": "gated"}  # base says eligible
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["write_kinds"]["draft_pr_create"]["ramp"] == "gated"


def test_refuses_skill_that_resolves_to_no_body(cartridges: Path) -> None:
    with pytest.raises(CartridgeError, match="resolves to no skill body"):
        load("acme", cartridges, skill_index={"acme-skills:plan": ["/fake/plan/SKILL.md"]})


def test_refuses_skill_that_resolves_to_two_bodies(cartridges: Path, skill_index) -> None:
    skill_index["acme-skills:plan"] = ["/one/SKILL.md", "/two/SKILL.md"]
    with pytest.raises(CartridgeError, match="resolves to 2 bodies"):
        load("acme", cartridges, skill_index=skill_index)


def test_refuses_missing_context_pack(cartridges: Path, skill_index) -> None:
    (cartridges / "acme" / "context" / "code-style.md").unlink()
    with pytest.raises(CartridgeError, match="context pack does not exist"):
        load("acme", cartridges, skill_index=skill_index)


def test_refuses_apply_arm_that_is_not_a_bound_role(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["write_kinds"]["ticket_create"] = {"apply_arm": "nobody_bound_this"}
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(CartridgeError, match="apply_arm 'nobody_bound_this', which is not a bound role"):
        load("acme", cartridges, skill_index=skill_index)


def test_shell_and_pr_are_valid_apply_arms_without_being_roles(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["write_kinds"]["ticket_create"] = {"apply_arm": "shell"}
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    assert load("acme", cartridges, skill_index=skill_index)["write_kinds"]["ticket_create"]["apply_arm"] == "shell"


def test_reports_every_problem_at_once(cartridges: Path) -> None:
    """One error per run is how people stop reading errors."""
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    del config["skills"]["build"]
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (cartridges / "acme" / "context" / "code-style.md").unlink()
    with pytest.raises(CartridgeError) as exc:
        load("acme", cartridges, skill_index={})
    message = str(exc.value)
    assert "required role 'build' is unbound" in message
    assert "context pack does not exist" in message
    assert "resolves to no skill body" in message


def test_refuses_inheritance_cycle(tmp_path: Path) -> None:
    root = tmp_path / "cartridges"
    write_cartridge(root / "a", {"team": "a", "extends": "b"})
    write_cartridge(root / "b", {"team": "b", "extends": "a"})
    with pytest.raises(CartridgeError, match="inheritance cycle"):
        load("a", root, skill_index={})


def test_refuses_missing_cartridge(tmp_path: Path) -> None:
    with pytest.raises(CartridgeError, match="no cartridge for 'ghost'"):
        load("ghost", tmp_path, skill_index={})


def test_loading_local_resolves_base_cartridges_plan_competition_min_tier() -> None:
    """Loads `local`, not `base`: `base` leaves required roles unbound and
    cannot resolve alone. `local` extends `base` and declares no `policy`
    block of its own, so the value asserted here is the one `base` sets.
    """
    resolved = load("local", REPO / "cartridges", skill_index=index_from_roots([REPO / "skills-plugins"]))
    assert resolved["policy"]["plan_competition"]["min_tier"] == 1
