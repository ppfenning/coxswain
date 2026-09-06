"""The demo contract: a clean clone of THIS repo can resolve `local`.

The `local` cartridge is the README's proof that a graph needs no tracker and
no vendor. That proof is only honest if the skills it binds actually ship —
which they did not, for a while: every binding pointed at a `local-skills`
plugin that existed nowhere, and the first thing a clean clone hit was twelve
resolution errors. These tests make that regression impossible to ship again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.cartridge import load
from core.skills import index_from_roots

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO / "skills-plugins"
PLUGIN = PLUGIN_ROOT / "local-skills"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"


def test_local_cartridge_resolves_against_the_inrepo_plugin() -> None:
    """The exact resolution a clean clone performs, minus nothing."""
    resolved = load("local", REPO / "cartridges", skill_index=index_from_roots([PLUGIN_ROOT]))
    assert resolved["team"] == "local"
    assert resolved["cartridge_sha"]


def test_every_graph_facing_role_is_bound() -> None:
    """`local` exists to run the graphs, so it binds what the graphs ask for.

    The graph repo asks for these roles by name; an unbound one fails at the
    runner, mid-run, which is exactly the too-late failure the loader exists
    to prevent. Kept as a literal list on purpose — if a graph grows a new
    role, adding it here is the reminder to bind and write the skill.
    """
    resolved = load("local", REPO / "cartridges", skill_index=index_from_roots([PLUGIN_ROOT]))
    graph_roles = {
        "plan", "build", "review_charter", "scope_epic", "handoff",
        "review_adversary", "arbitrate", "decompose", "evidence_verify",
        "plan_alternative", "plan_arbitrate", "plan_adversary",
        "triage_classify", "reconcile",
        "validate_chunk", "validate_phase", "retro", "dispatch", "route",
    }
    arm_roles = {"work_state_arm", "work_item_arm", "docs_apply_arm"}
    unbound = (graph_roles | arm_roles) - set(resolved["skills"])
    assert not unbound, f"local leaves graph-facing roles unbound: {sorted(unbound)}"


def test_skill_bodies_are_real_documents_not_stubs() -> None:
    """A body exists, names itself correctly, and carries actual guidance."""
    bodies = sorted(PLUGIN.glob("skills/*/SKILL.md"))
    assert len(bodies) >= 18
    for body in bodies:
        text = body.read_text(encoding="utf-8")
        match = re.match(r"^---\nname: (\S+)\n", text)
        assert match, f"{body}: missing frontmatter name"
        assert match.group(1) == body.parent.name, f"{body}: frontmatter name != directory"
        assert len(text.splitlines()) >= 25, f"{body}: too short to be real guidance"


def test_route_work_checks_the_leader_before_routing_anything() -> None:
    raw = (PLUGIN / "skills" / "route-work" / "SKILL.md").read_text(encoding="utf-8")
    text = " ".join(raw.split())
    assert "cox route leader status" in text
    assert "cox route leader take" in text
    assert "do not re-arm" in text
    assert "naming your own label" in text
    assert "leader lock still names you" in text


def test_route_work_stops_rather_than_clearing_an_unavailable_leader_check() -> None:
    raw = (PLUGIN / "skills" / "route-work" / "SKILL.md").read_text(encoding="utf-8")
    text = " ".join(raw.split())
    assert "errors or is not yet installed" in text
    assert "say the check is unavailable and stop there too" in text
    assert "do not treat a failed or missing check as a clear lock" in text
    assert "proceed as if no lock exists" not in text


def test_the_index_maps_each_binding_to_exactly_one_body() -> None:
    index = index_from_roots([PLUGIN_ROOT])
    resolved = load("local", REPO / "cartridges", skill_index=index)
    for role, name in resolved["skills"].items():
        assert len(index.get(name, ())) == 1, f"{role} -> {name} is not uniquely resolvable"


def test_the_marketplace_manifest_lists_local_skills_at_a_real_path() -> None:
    """`claude plugin marketplace add` reads this file; a dangling source is silent until install time."""
    manifest = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    plugins = {plugin["name"]: plugin for plugin in manifest["plugins"]}
    assert "local-skills" in plugins
    source = plugins["local-skills"]["source"]
    marketplace_root = MARKETPLACE.parent.parent
    resolved = (marketplace_root / source).resolve()
    assert resolved.is_dir()
    assert resolved == PLUGIN.resolve()
