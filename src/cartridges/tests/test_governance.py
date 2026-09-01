"""The SHIPPED taxonomy and the comfort presets, resolved the way a clone does.

Everything else about the loader is tested against synthetic fixtures, and
should be. These tests are different on purpose: the write-kind taxonomy in
`cartridges/base/` is not an example of a taxonomy, it is the one that governs
whether anything may write, and the comfort bundles are the artefact a team
actually points at. If a rename or a well-meant tidy loosened one of them, no
fixture would notice.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.cartridge import CartridgeError, load
from core.skills import index_from_roots
from tests.conftest import write_cartridge

REPO = Path(__file__).resolve().parent.parent
CARTRIDGES = REPO / "cartridges"
PLUGIN_ROOT = REPO / "skills-plugins"


def resolve(team: str, cartridges_dir: Path = CARTRIDGES) -> dict:
    return load(team, cartridges_dir, skill_index=index_from_roots([PLUGIN_ROOT]))


# ── the split of `merge` by target ─────────────────────────────────────────


def test_merge_is_split_by_target_not_left_as_one_blunt_kind() -> None:
    kinds = resolve("local")["write_kinds"]
    assert "merge" not in kinds, "the undifferentiated kind is retired, not kept alongside"
    assert kinds["merge_stack"] == {"risk": "high", "ramp": "eligible", "apply_arm": "shell"}
    assert kinds["merge_main"]["risk"] == "high"
    assert kinds["merge_main"]["ramp"] == "never", "merge_main inherits the old kind's posture exactly"
    assert "apply_arm" not in kinds["merge_main"], "no arm: it goes to the gate and a human runs it"


def test_the_branch_rewriting_and_self_governing_kinds_exist_with_their_ramps() -> None:
    kinds = resolve("local")["write_kinds"]
    assert kinds["stack_rebase"]["ramp"] == "eligible", "earnable, because 'after fifty clean ones' is the point"
    assert kinds["self_modification"]["ramp"] == "never"
    assert kinds["self_modification"]["apply_arm"] == "pr", "a system may not loosen its own rules in place"


def test_the_new_validation_roles_are_declared_optional() -> None:
    optional = set(resolve("local")["roles"]["optional"])
    assert {"validate_chunk", "validate_phase", "retro", "dispatch"} <= optional


def test_a_team_may_not_loosen_merge_main_off_never(tmp_path: Path) -> None:
    """The one kind whose whole value is that no streak can ever buy it."""
    root = tmp_path / "cartridges"
    shutil.copytree(CARTRIDGES, root)
    write_cartridge(
        root / "reckless",
        {"team": "reckless", "extends": "local", "version": 1, "write_kinds": {"merge_main": {"ramp": "eligible"}}},
    )
    with pytest.raises(CartridgeError, match="loosens merge_main.ramp from 'never' to 'eligible'"):
        resolve("reckless", root)


def test_a_team_may_tighten_merge_stack_to_gated(tmp_path: Path) -> None:
    """The human gate toggle: legal precisely because tightening is one-way."""
    root = tmp_path / "cartridges"
    shutil.copytree(CARTRIDGES, root)
    write_cartridge(
        root / "careful",
        {"team": "careful", "extends": "local", "version": 1, "write_kinds": {"merge_stack": {"ramp": "gated"}}},
    )
    assert resolve("careful", root)["write_kinds"]["merge_stack"]["ramp"] == "gated"


# ── comfort presets are ramp bundles, and nothing more ─────────────────────

EARNABLE = ("draft_pr_create", "retry_idempotent", "merge_stack", "stack_rebase")
DEFERRED = ("item_create", "item_update", "state_move", "doc_update")


def test_comfort0_pins_every_earnable_and_deferred_kind_to_the_gate() -> None:
    kinds = resolve("local-comfort0")["write_kinds"]
    for kind in EARNABLE + DEFERRED:
        assert kinds[kind]["ramp"] == "gated", f"{kind} can still graduate at comfort 0"


def test_comfort1_holds_back_only_the_branch_kinds() -> None:
    kinds = resolve("local-comfort1")["write_kinds"]
    assert kinds["merge_stack"]["ramp"] == "gated"
    assert kinds["stack_rebase"]["ramp"] == "gated"
    assert kinds["draft_pr_create"]["ramp"] == "eligible", "the cheap-to-be-wrong kinds still earn"
    assert kinds["retry_idempotent"]["ramp"] == "eligible"


@pytest.mark.parametrize("team", ["local-comfort0", "local-comfort1"])
def test_a_preset_tightens_ramps_and_touches_nothing_else(team: str) -> None:
    """A comfort level is not new machinery — same bindings, same risks, same packs."""
    preset, base = resolve(team), resolve("local")
    assert preset["skills"] == base["skills"], "presets inherit local's bindings whole"
    assert preset["context"] == base["context"], "no pack of their own: nothing extra concatenates"
    for kind, spec in preset["write_kinds"].items():
        assert spec.get("risk") == base["write_kinds"][kind].get("risk"), f"{team} restated {kind}'s risk"
    assert preset["cartridge_sha"] != base["cartridge_sha"], "different rules, so a different track record"
