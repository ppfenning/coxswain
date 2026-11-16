"""Decompose item_create proposals carry the payload the workstore arm writes in code."""

from __future__ import annotations

import pytest
import yaml

from core import workstore
from graphs.delivery import initiative_decompose as decompose
from harness.gate import auto_apply, workstore_route
from runner import ScriptedRunner

PLAN = {
    "phases": [{"id": "p1", "goal": "foundations"}, {"id": "p2", "goal": "cutover"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "title": "schema: probe", "body": "first", "needs": [], "surfaces": ["a.py"]},
        {"id": "t2", "phase": "p2", "title": "cutover", "body": "second", "needs": ["t1"], "surfaces": ["b.py"]},
    ],
    "rationale": "two steps",
}

# A body ending in the seat's closing tag, and an empty body: the two cases where the cleaned
# body differs from the raw one.
EDGE_PLAN = {
    **PLAN,
    "tasks": [
        {**PLAN["tasks"][0], "body": "first\n</output>", "surfaces": []},
        {**PLAN["tasks"][1], "body": ""},
    ],
}

STORED = ("id", "title", "phase", "state", "needs", "surfaces")


class ForbiddenRunner:
    def run(self, **_: object) -> None:
        pytest.fail("the workstore arm must not call the model")


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["decompose"] = "acme-skills:decompose"
    cartridge["work_routing"] = {"states": {"active": "work", "planned": "work", "future": "backlog"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "eligible", "apply_arm": "workstore"}
    return cartridge


def run_plan(cart: dict, plan: dict) -> dict:
    args = {"run_id": "r", "date": "2026-11-10", "cartridge": cart, "idea": "go arrow-native", "initiative_id": "init"}
    return decompose.run(args, ScriptedRunner({"decompose": plan}))


@pytest.fixture
def result(cart) -> dict:
    return run_plan(cart, PLAN)


def rendered_frontmatter(action: str) -> dict:
    """The frontmatter block of a ticket's prose action, parsed."""
    return yaml.safe_load(action.split("with frontmatter\n", 1)[1].rsplit("body = the rationale", 1)[0])


def without_body(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "body"}


def test_every_item_create_routes_to_the_workstore_arm(result) -> None:
    assert len(result["proposals"]) == 3
    for p in result["proposals"]:
        assert isinstance(p["apply"]["path"], str)
        assert isinstance(p["apply"]["item"], dict)
        assert workstore_route(p["kind"], p["apply"])[0] == "item_create"
    assert result["proposals"][0]["apply"]["path"] == "work/init/p1/init-t1.md"
    assert result["proposals"][2]["apply"]["path"] == "work/init/initiative.md"


def test_the_prose_action_names_the_same_path_and_frontmatter_as_the_payload(result) -> None:
    for p in result["proposals"][:2]:
        assert p["suggested_action"].startswith(f"create {p['apply']['path']} with frontmatter\n")
        assert rendered_frontmatter(p["suggested_action"]) == without_body(p["apply"]["item"])


def test_the_initiative_prose_and_payload_agree_on_frontmatter_and_body(result) -> None:
    initiative = result["proposals"][2]
    prose = initiative["suggested_action"].split("with body =\n", 1)[1]
    _, front, body = prose.split("---\n", 2)
    assert yaml.safe_load(front) == without_body(initiative["apply"]["item"])
    assert body == "\n" + initiative["apply"]["item"]["body"]


def test_auto_apply_writes_both_tickets_and_the_initiative(result, cart, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for p in result["proposals"]:
        assert auto_apply(p, cartridge=cart, runner=ForbiddenRunner())[0] is True
    for p in result["proposals"][:2]:
        rendered = rendered_frontmatter(p["suggested_action"])
        read = workstore.read_item(tmp_path / p["apply"]["path"])
        assert {k: read[k] for k in STORED} == {k: rendered[k] for k in STORED}
        assert read["body"] == p["apply"]["item"]["body"]
        # Expected loss: write_item keeps no `lint` (and no `checks`), though the action renders `lint`.
        assert "lint" in rendered
        assert "lint" not in read
    initiative = workstore.read_item(tmp_path / "work/init/initiative.md")
    assert initiative["id"] == "init"
    assert initiative["body"] == result["proposals"][2]["apply"]["item"]["body"].strip()
    assert "---" not in initiative["body"]


@pytest.mark.parametrize("plan", [PLAN, EDGE_PLAN], ids=["plain", "trailing-tag-and-empty"])
def test_the_payload_agrees_with_the_plan_task(cart, plan) -> None:
    route = pytest.importorskip("agent_tools.route")
    result = run_plan(cart, plan)
    plan_tasks = route.plan_from_result(result, "init", "title", "body")["tasks"]
    items = {p["apply"]["item"]["id"]: p["apply"]["item"] for p in result["proposals"][:2]}
    assert sorted(items) == ["init-t1", "init-t2"]
    assert len(plan_tasks) == 2
    for task in plan_tasks:
        item = items[task["id"]]
        for key in ("id", "phase", "needs", "surfaces", "title", "body"):
            assert item[key] == task[key]
    for p in result["proposals"][:2]:
        assert p["rationale"] == p["apply"]["item"]["body"]


def test_a_trailing_tag_is_stripped_and_an_empty_body_takes_the_rationale(cart) -> None:
    bodies = {p["target"]: p["apply"]["item"]["body"] for p in run_plan(cart, EDGE_PLAN)["proposals"][:2]}
    assert bodies == {"init-t1": "first", "init-t2": "two steps"}


@pytest.mark.parametrize("apply", [None, {"path": "work/x.md"}, {"path": "work/x.md", "item": "text"}])
def test_an_apply_without_an_item_routes_to_fallback(apply) -> None:
    assert workstore_route("item_create", apply) == ("fallback", {})


def test_a_proposal_with_its_item_removed_routes_to_fallback(result) -> None:
    apply = {k: v for k, v in result["proposals"][0]["apply"].items() if k != "item"}
    assert "item" not in apply
    assert workstore_route("item_create", apply) == ("fallback", {})
