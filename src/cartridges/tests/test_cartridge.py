"""The loader must refuse bad cartridges AT LOAD, and say everything that is wrong."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from core.cartridge import (
    DERIVED_KEYS,
    CartridgeError,
    _fold_fragments,
    _review_tier_problems,
    apply_overlay,
    layers,
    load,
    overlay_errors,
)
from core.skills import index_from_roots
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


def test_layers_of_a_lone_cartridge_is_one_entry_labelled_by_its_name(tmp_path: Path) -> None:
    root = tmp_path / "cartridges"
    write_cartridge(root / "base", {"team": "base", "version": 1})
    resolved = layers("base", root, skill_index={})
    assert [label for label, _ in resolved] == ["base"]
    assert resolved[-1][1] == load("base", root, skill_index={})


def test_layers_of_base_plus_acme_is_two_entries_ending_in_load(cartridges: Path, skill_index) -> None:
    resolved = layers("acme", cartridges, skill_index=skill_index)
    assert [label for label, _ in resolved] == ["base", "acme"]
    assert resolved[-1][1] == load("acme", cartridges, skill_index=skill_index)


def test_layers_of_acme_with_two_fragments_is_four_entries_each_showing_its_own_change(
    cartridges: Path, skill_index
) -> None:
    _write_fragment(cartridges / "acme", "10-first.yaml", {"beta": "on"})
    _write_fragment(cartridges / "acme", "20-second.yaml", {"gamma": "on"})
    resolved = layers("acme", cartridges, skill_index=skill_index)
    assert [label for label, _ in resolved] == [
        "base",
        "acme",
        "acme/cartridge.d/10-first.yaml",
        "acme/cartridge.d/20-second.yaml",
    ]
    before_first = resolved[1][1]
    after_first = resolved[2][1]
    after_second = resolved[3][1]
    assert "beta" not in before_first
    assert after_first["beta"] == "on"
    assert "gamma" not in after_first
    assert after_second["gamma"] == "on"
    assert resolved[-1][1] == load("acme", cartridges, skill_index=skill_index)


def test_layers_raises_the_same_error_load_raises_on_an_illegal_loosen(cartridges: Path, skill_index) -> None:
    _write_fragment(cartridges / "acme", "10-loosen.yaml", {"write_kinds": {"merge": {"risk": "low"}}})
    with pytest.raises(CartridgeError, match="loosens merge.risk from 'high' to 'low'"):
        layers("acme", cartridges, skill_index=skill_index)


def test_a_team_declaring_crew_resolves_with_no_deprecations(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["crew"] = {"nova": {"skills": []}}
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["crew"] == {"nova": {"skills": []}}
    assert resolved["deprecations"] == []


def test_a_team_declaring_cast_resolves_to_the_same_crew_value_with_one_deprecation(
    cartridges: Path, skill_index
) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["cast"] = {"nova": {"skills": []}}
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["crew"] == {"nova": {"skills": []}}
    assert resolved["deprecations"] == ["acme: rename cast to crew"]


def test_a_layer_declaring_both_cast_and_crew_is_refused(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["cast"] = {"nova": {"skills": []}}
    config["crew"] = {"sky": {"skills": []}}
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(CartridgeError, match="acme: declares both 'cast' and 'crew'"):
        load("acme", cartridges, skill_index=skill_index)


def test_a_fragment_using_cast_is_accepted_and_named_in_deprecations(cartridges: Path, skill_index) -> None:
    _write_fragment(cartridges / "acme", "10-cast.yaml", {"cast": {"nova": {"skills": []}}})
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["crew"] == {"nova": {"skills": []}}
    assert resolved["deprecations"] == ["acme/cartridge.d/10-cast.yaml: rename cast to crew"]


def test_the_resolved_dict_has_cast_equal_to_crew(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["crew"] = {"nova": {"skills": []}}
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    resolved = load("acme", cartridges, skill_index=skill_index)
    assert resolved["cast"] == resolved["crew"]


def test_overlay_errors_names_the_refused_key() -> None:
    assert overlay_errors({"skills": {"plan": "x"}}) == ["project layer overlay refuses key 'skills'"]


def test_overlay_errors_refuses_unknown_nested_keys() -> None:
    overlay = {"policy": {"merge_main": "eligible"}, "landing_areas": {"active_work": "foo"}}
    problems = overlay_errors(overlay)
    assert "project layer overlay refuses key 'policy.merge_main'" in problems
    assert "project layer overlay refuses key 'landing_areas.active_work'" in problems


def test_overlay_errors_refuses_a_non_list_context() -> None:
    assert overlay_errors({"context": "style.md"}) == ["overlay key 'context' must be a list, got str"]


def test_apply_overlay_adds_a_context_file_and_a_tier2_surface() -> None:
    resolved = {"context": ["/repo/base.md"], "policy": {"review_tier": {"tier2_surfaces": ["schema"]}}}
    overlay = {"context": ["/repo/overlay.md"], "policy": {"review_tier": {"tier2_surfaces": ["schema", "auth"]}}}
    merged = apply_overlay(resolved, overlay)
    assert merged["context"] == ["/repo/base.md", "/repo/overlay.md"]
    assert merged["policy"]["review_tier"]["tier2_surfaces"] == ["schema", "auth"]


def test_apply_overlay_sets_description_and_merges_landing_areas_checks() -> None:
    resolved = {"description": "old", "landing_areas": {"active_work": "board"}}
    overlay = {"description": "new", "landing_areas": {"checks": ["lint"]}}
    merged = apply_overlay(resolved, overlay)
    assert merged["description"] == "new"
    assert merged["landing_areas"] == {"active_work": "board", "checks": ["lint"]}


def test_apply_overlay_of_none_returns_the_input_unchanged() -> None:
    resolved = {"context": ["/repo/base.md"]}
    assert apply_overlay(resolved, None) == resolved


def test_review_tier_problems_reports_a_raised_threshold() -> None:
    base = {"tier1_max_changed_lines": 150}
    overlay = {"tier1_max_changed_lines": 200}
    assert _review_tier_problems(base, overlay) == [
        "overlay raises review_tier.tier1_max_changed_lines from 150 to 200; a project layer may only lower a threshold"
    ]


def test_review_tier_problems_refuses_a_non_numeric_threshold() -> None:
    base = {"tier1_max_changed_lines": 150}
    overlay = {"tier1_max_changed_lines": "999"}
    assert _review_tier_problems(base, overlay) == [
        "overlay sets review_tier.tier1_max_changed_lines to str, not a number"
    ]


def test_review_tier_problems_reports_a_dropped_surface() -> None:
    base = {"tier2_surfaces": ["schema", "auth"]}
    overlay = {"tier2_surfaces": ["schema"]}
    assert _review_tier_problems(base, overlay) == [
        "overlay drops tier2_surfaces ['auth']; a project layer may only add entries"
    ]


def test_review_tier_problems_refuses_a_non_list_tier2_surfaces() -> None:
    base = {"tier2_surfaces": ["auth"]}
    overlay = {"tier2_surfaces": "authz"}
    assert _review_tier_problems(base, overlay) == ["overlay sets tier2_surfaces to str, not a list"]


def test_review_tier_problems_reports_an_added_tier0_pattern() -> None:
    base = {"tier0_patterns": ["docs_only"]}
    overlay = {"tier0_patterns": ["docs_only", "config_bump"]}
    assert _review_tier_problems(base, overlay) == [
        "overlay adds tier0_patterns ['config_bump']; a project layer may only remove entries"
    ]


def test_load_with_overlay_none_matches_a_call_with_no_overlay_argument(cartridges: Path, skill_index) -> None:
    explicit_none = load("acme", cartridges, skill_index=skill_index, overlay=None)
    omitted = load("acme", cartridges, skill_index=skill_index)
    assert explicit_none == omitted
    assert explicit_none["overlay_sha"] is None


def test_cartridge_sha_with_no_overlay_equals_the_payload_and_context_hash(cartridges: Path, skill_index) -> None:
    resolved = load("acme", cartridges, skill_index=skill_index)
    payload = {k: v for k, v in resolved.items() if k not in DERIVED_KEYS and k != "context"}
    digest = hashlib.sha256()
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
    for path in resolved["context"]:
        digest.update(b"\0")
        digest.update(Path(path).read_bytes())
    assert digest.hexdigest() == resolved["cartridge_sha"]


def test_load_reports_every_overlay_problem_not_just_the_first(cartridges: Path, skill_index) -> None:
    config = yaml.safe_load((cartridges / "acme" / "cartridge.yaml").read_text())
    config["policy"] = {"review_tier": {"tier1_max_changed_lines": 10}}
    (cartridges / "acme" / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    overlay = {"skills": {"x": "y"}, "policy": {"review_tier": {"tier1_max_changed_lines": 999}}}
    with pytest.raises(CartridgeError) as exc:
        load("acme", cartridges, skill_index=skill_index, overlay=overlay)
    message = str(exc.value)
    assert "refuses key 'skills'" in message
    assert "raises review_tier.tier1_max_changed_lines" in message


def test_load_applies_an_overlay_context_file_and_changes_the_sha(tmp_path: Path, cartridges: Path, skill_index) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "ops.md").write_text("ops charter\n", encoding="utf-8")
    without_overlay = load("acme", cartridges, skill_index=skill_index)
    overlay = {"context": ["ops.md"], "policy": {"review_tier": {"tier2_surfaces": ["schema"]}}}
    resolved = load("acme", cartridges, skill_index=skill_index, overlay=overlay, overlay_dir=project_dir)
    assert Path(resolved["context"][-1]) == (project_dir / "ops.md").resolve()
    assert resolved["policy"]["review_tier"]["tier2_surfaces"] == ["schema"]
    assert resolved["cartridge_sha"] != without_overlay["cartridge_sha"]
    assert resolved["overlay_sha"] is not None


def test_load_refuses_an_overlay_context_file_that_does_not_exist(cartridges: Path, skill_index) -> None:
    overlay = {"context": ["missing.md"]}
    with pytest.raises(CartridgeError, match="context pack does not exist"):
        load("acme", cartridges, skill_index=skill_index, overlay=overlay, overlay_dir=cartridges)


def test_a_non_mapping_review_tier_is_an_overlay_error():
    assert overlay_errors({"policy": {"review_tier": "tight"}}) == ["overlay key 'policy.review_tier' must be a mapping, got str"]
