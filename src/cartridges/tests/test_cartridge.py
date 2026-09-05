"""The loader must refuse bad cartridges AT LOAD, and say everything that is wrong."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.skills import index_from_roots
from core.cartridge import CartridgeError, _fold_fragments, load
from tests.conftest import write_cartridge

REPO = Path(__file__).resolve().parent.parent


def _write_fragment(directory: Path, name: str, config: dict) -> Path:
    """A `cartridge.d/<name>.yaml` fragment, written directly for one test."""
    frag_dir = directory / "cartridge.d"
    frag_dir.mkdir(parents=True, exist_ok=True)
    path = frag_dir / name
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


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


def test_fold_fragments_is_pure_and_takes_literals() -> None:
    """No filesystem, no cartridge — plain dicts in, plain dict and problems out."""
    layer = {"team": "x", "write_kinds": {"merge": {"risk": "medium"}}}
    fragments = [
        ("frag-a.yaml", {"team": "y"}),
        ("frag-b.yaml", {"write_kinds": {"merge": {"risk": "high"}}}),
    ]
    folded, problems = _fold_fragments(layer, fragments, layer)
    assert problems == []
    assert folded["team"] == "y"
    assert folded["write_kinds"]["merge"]["risk"] == "high"


def test_fold_fragments_reports_one_loosening_named_by_fragment_label() -> None:
    layer = {"write_kinds": {"merge": {"risk": "high"}}}
    fragments = [
        ("10-tighten.yaml", {"write_kinds": {"merge": {"risk": "high"}}}),
        ("20-loosen.yaml", {"write_kinds": {"merge": {"risk": "low"}}}),
    ]
    _, problems = _fold_fragments(layer, fragments, layer)
    assert len(problems) == 1, "each illegal loosen is reported exactly once"
    assert "20-loosen.yaml" in problems[0]
    assert "loosens merge.risk from 'high' to 'low'" in problems[0]


def test_a_fragment_overrides_a_scalar(cartridges: Path, skill_index) -> None:
    _write_fragment(cartridges / "acme", "10-override.yaml", {"team": "acme-fragment"})
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["team"] == "acme-fragment"


def test_a_fragment_tightens_a_risk_field(cartridges: Path, skill_index) -> None:
    _write_fragment(
        cartridges / "acme", "10-tighten.yaml", {"write_kinds": {"draft_pr_create": {"risk": "medium"}}}
    )  # base says low
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["write_kinds"]["draft_pr_create"]["risk"] == "medium"


def test_a_fragment_illegally_loosening_a_risk_field_is_refused(cartridges: Path, skill_index) -> None:
    _write_fragment(cartridges / "acme", "10-loosen.yaml", {"write_kinds": {"merge": {"risk": "low"}}})  # base says high
    with pytest.raises(CartridgeError, match="loosens merge.risk from 'high' to 'low'"):
        load("acme", cartridges, skill_index=skill_index)


def test_a_fragment_reverting_the_teams_own_tightening_is_refused(cartridges: Path, skill_index) -> None:
    """A fragment is checked against the accumulated authority, which includes
    the team's own `cartridge.yaml` — not only the parent chain.

    The parent (base) already says `eligible`, and the fragment's own value
    (`eligible`) matches it exactly — a parent-only check would see no
    loosening. It is a loosening against the TEAM's `cartridge.yaml`, which
    tightened to `gated`, so the fragment must be caught there.
    """
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["write_kinds"]["draft_pr_create"] = {"ramp": "gated"}  # tightened above base's eligible
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    frag = _write_fragment(
        cartridges / "acme", "20-revert.yaml", {"write_kinds": {"draft_pr_create": {"ramp": "eligible"}}}
    )
    with pytest.raises(CartridgeError, match="loosens draft_pr_create.ramp from 'gated' to 'eligible'") as exc:
        load("acme", cartridges, skill_index=skill_index)
    assert frag.name in str(exc.value), "the error must name the reverting fragment"


def test_a_fragment_loosening_a_kind_the_parent_never_declared_is_refused(cartridges: Path, skill_index) -> None:
    """Base is silent on `epic_create` entirely — a parent-only authority would
    have nothing to compare against. The team's own `cartridge.yaml` tightens
    it, and that is the authority the fragment must answer to.
    """
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["write_kinds"]["epic_create"] = {"risk": "high"}
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    frag = _write_fragment(cartridges / "acme", "10-loosen.yaml", {"write_kinds": {"epic_create": {"risk": "low"}}})
    with pytest.raises(CartridgeError, match="loosens epic_create.risk from 'high' to 'low'") as exc:
        load("acme", cartridges, skill_index=skill_index)
    assert frag.name in str(exc.value)


def test_two_fragments_second_reverting_the_firsts_tightening_is_refused(cartridges: Path, skill_index) -> None:
    """Sorted filename order and the running authority both matter here:
    `10-a.yaml` tightens first and must be allowed to stand as the new
    authority; only then does `20-b.yaml` loosen it back, and only then is
    it a problem.
    """
    _write_fragment(cartridges / "acme", "10-a.yaml", {"write_kinds": {"draft_pr_create": {"ramp": "gated"}}})
    frag_b = _write_fragment(
        cartridges / "acme", "20-b.yaml", {"write_kinds": {"draft_pr_create": {"ramp": "eligible"}}}
    )
    with pytest.raises(CartridgeError, match="loosens draft_pr_create.ramp from 'gated' to 'eligible'") as exc:
        load("acme", cartridges, skill_index=skill_index)
    assert frag_b.name in str(exc.value)


def test_a_base_fragment_folds_into_the_resolved_leaf_and_changes_the_sha(cartridges: Path, skill_index) -> None:
    """Fragments fold at EVERY layer of the chain, not only the leaf team."""
    before = load("acme", cartridges, skill_index=skill_index)["cartridge_sha"]
    _write_fragment(cartridges / "base", "10-base.yaml", {"write_kinds": {"draft_pr_create": {"risk": "medium"}}})
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["write_kinds"]["draft_pr_create"]["risk"] == "medium"
    assert resolved["cartridge_sha"] != before


def test_an_empty_fragment_resolves_as_if_absent(cartridges: Path, skill_index) -> None:
    frag_dir = cartridges / "acme" / "cartridge.d"
    frag_dir.mkdir(parents=True, exist_ok=True)
    (frag_dir / "10-empty.yaml").write_text("# just a comment, no mapping here\n", encoding="utf-8")
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["team"] == "acme"
    assert resolved["write_kinds"]["merge"]["risk"] == "high"


def test_a_fragment_adds_a_context_path(cartridges: Path, skill_index) -> None:
    (cartridges / "acme" / "context" / "extra.md").write_text("extra pack\n", encoding="utf-8")
    _write_fragment(cartridges / "acme", "10-context.yaml", {"context": ["context/extra.md"]})
    resolved = load("acme", cartridges, skill_index=skill_index)
    names = [Path(p).name for p in resolved["context"]]
    assert names == ["conventions.md", "code-style.md", "extra.md"]


def test_a_team_with_no_cartridge_d_resolves_exactly_as_before(cartridges: Path, skill_index) -> None:
    assert not (cartridges / "acme" / "cartridge.d").exists()
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["team"] == "acme"
    assert resolved["write_kinds"]["merge"]["risk"] == "high"


def test_sha_changes_when_a_fragment_changes_content(cartridges: Path, skill_index) -> None:
    """Both edits below leave the MERGED config byte-identical to before —
    `graduation_n: 3` matches base's own value, and a trailing comment
    parses to the same value again — so the only thing that can move the
    sha is the fragment's own bytes being hashed directly.
    """
    before = load("acme", cartridges, skill_index=skill_index)["cartridge_sha"]
    frag = _write_fragment(cartridges / "acme", "10-sha.yaml", {"policy": {"graduation_n": 3}})
    after_add = load("acme", cartridges, skill_index=skill_index)["cartridge_sha"]
    assert before != after_add, "adding a fragment must change the hash even though the merged value is unchanged"
    frag.write_text("policy:\n  graduation_n: 3  # comment-only edit, parses to the same value\n", encoding="utf-8")
    after_edit = load("acme", cartridges, skill_index=skill_index)["cartridge_sha"]
    assert after_add != after_edit, "editing a fragment's bytes must change the hash even when the parsed value does not"


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


def test_base_cartridge_bounds_dispatch_concurrency() -> None:
    """Loads `local`, not `base`, for the same reason as above: `local`
    declares no `policy` block, so the value asserted here is `base`'s.
    """
    resolved = load("local", REPO / "cartridges", skill_index=index_from_roots([REPO / "skills-plugins"]))
    assert resolved["policy"]["dispatch"]["max_in_flight"] == 3
