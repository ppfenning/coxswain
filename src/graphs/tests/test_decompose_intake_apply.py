"""The decompose's intake state_move carries the retire payload the workstore arm applies in code."""

from __future__ import annotations

import pytest
import yaml

from graphs.delivery import initiative_decompose as decompose
from harness.gate import auto_apply, workstore_route
from runner import ScriptedRunner

PLAN = {
    "phases": [{"id": "p1", "goal": "foundations"}, {"id": "p2", "goal": "cutover"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "title": "probe", "body": "first", "needs": [], "surfaces": ["a.py"]},
        {"id": "t2", "phase": "p2", "title": "cutover", "body": "second", "needs": ["t1"], "surfaces": ["b.py"]},
    ],
    "rationale": "two steps",
}

INTAKE = "---\nid: regatta\ntitle: Go arrow-native\nrepo: ~/repos/r\n---\n\nThe idea, in full.\n"


class ForbiddenRunner:
    def run(self, **_: object) -> None:
        pytest.fail("the workstore arm must not call the model")


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["decompose"] = "acme-skills:decompose"
    cartridge["work_routing"] = {"states": {"active": "work", "planned": "work", "future": "backlog"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "eligible", "apply_arm": "workstore"}
    cartridge["write_kinds"]["state_move"] = {"risk": "low", "ramp": "eligible", "apply_arm": "workstore"}
    return cartridge


def run_plan(cart: dict, **extra: object) -> dict:
    args = {"run_id": "r-1", "date": "2026-11-16", "cartridge": cart, "idea": "go arrow-native", **extra}
    return decompose.run(args, ScriptedRunner({"decompose": PLAN}))


def intake_move(result: dict) -> dict:
    (move,) = [p for p in result["proposals"] if p["kind"] == "state_move"]
    return move


def test_the_intake_state_move_carries_the_retire_payload(cart) -> None:
    move = intake_move(run_plan(cart, initiative_id="init", intake_path="intake/regatta.md"))
    assert move["apply"] == {
        "path": "intake/regatta.md",
        "retire_to": "intake/done/regatta.md",
        "fields": {"initiative": "init"},
    }
    assert workstore_route(move["kind"], move["apply"])[0] == "retire"
    assert move["suggested_action"].startswith("call `cox route file --from-intake intake/regatta.md`")


def test_with_no_initiative_id_the_run_id_is_stamped_as_the_prose_says(cart) -> None:
    move = intake_move(run_plan(cart, intake_path="intake/regatta.md"))
    assert move["apply"]["fields"] == {"initiative": "r-1"}
    assert "`initiative: r-1`" in move["suggested_action"]


def test_the_retire_name_is_the_intake_file_name_alone(cart) -> None:
    move = intake_move(run_plan(cart, initiative_id="init", intake_path="nested/intake/regatta.md"))
    assert move["apply"]["retire_to"] == "intake/done/regatta.md"


def test_no_intake_path_emits_no_state_move(cart) -> None:
    result = run_plan(cart, initiative_id="init")
    assert not any(p["kind"] == "state_move" for p in result["proposals"])


def test_auto_apply_writes_every_item_and_retires_the_intake_with_no_model_call(cart, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "regatta.md").write_text(INTAKE, encoding="utf-8")
    result = run_plan(cart, initiative_id="init", intake_path="intake/regatta.md")
    assert [p["kind"] for p in result["proposals"]] == ["item_create", "item_create", "item_create", "state_move"]
    for p in result["proposals"]:
        assert auto_apply(p, cartridge=cart, runner=ForbiddenRunner())[0] is True
    assert not (tmp_path / "intake" / "regatta.md").exists()
    text = (tmp_path / "intake" / "done" / "regatta.md").read_text(encoding="utf-8")
    retired = yaml.safe_load(text.split("---\n", 2)[1])
    assert retired == {"id": "regatta", "title": "Go arrow-native", "repo": "~/repos/r", "initiative": "init"}
    assert text.endswith("\n\nThe idea, in full.\n")
    for rel in ("work/init/p1/init-t1.md", "work/init/p2/init-t2.md", "work/init/initiative.md"):
        assert (tmp_path / rel).is_file(), rel
