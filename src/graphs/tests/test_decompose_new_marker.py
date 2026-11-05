from __future__ import annotations

import pytest

from graphs.delivery import initiative_decompose
from runner import ScriptedRunner

TREE = [{"repo": "r", "path": "src/widget.py"}, {"repo": "r", "path": "src/app.py"}]
HEALED = "healed by the surface check"


def task(id_: str, body: str, surfaces: list[str]) -> dict:
    return {"id": id_, "phase": "p1", "title": id_, "body": body, "surfaces": surfaces, "needs": []}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["decompose"] = "acme-skills:decompose"
    cartridge["work_routing"] = {"states": {"active": "work", "planned": "work", "future": "backlog"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "deferred"}
    cartridge["write_kinds"]["state_move"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


def decompose(cart: dict, tasks: list[dict], listing: list[dict], monkeypatch) -> dict:
    """Runs the decompose with no --tree, so the checkout listing (`listing`) is the only tree in scope."""
    monkeypatch.setattr(initiative_decompose, "repo_listing", lambda repo: listing)
    runner = ScriptedRunner({"decompose": [{"phases": [{"id": "p1", "goal": "g"}], "tasks": tasks}]})
    args = {"run_id": "r", "date": "d", "idea": "x", "cartridge": cart}
    result = initiative_decompose.run(args, runner)
    return next(t for t in result["tasks"] if t["id"] == "a")


def test_a_task_naming_two_missing_files_comes_out_with_both_marked_and_two_notes(cart, monkeypatch):
    body = "Add src/one.py and src/two.py."
    got = decompose(cart, [task("a", body, ["src/one.py", "src/two.py"])], TREE, monkeypatch)
    assert got["surfaces"] == ["src/one.py (new)", "src/two.py (new)"]
    notes = [n for n in got["lint"] if HEALED in n]
    assert len(notes) == 2
    assert notes[0].startswith("src/one.py:") and notes[1].startswith("src/two.py:")


def test_a_task_naming_an_existing_file_is_unchanged_and_has_no_new_lint(cart, monkeypatch):
    got = decompose(cart, [task("a", "Edit src/widget.py.", ["src/widget.py"])], TREE, monkeypatch)
    assert got["surfaces"] == ["src/widget.py"]
    assert not any(HEALED in n for n in got.get("lint") or [])


def test_an_empty_tree_leaves_surfaces_unchanged(cart, monkeypatch):
    got = decompose(cart, [task("a", "Add src/one.py.", ["src/one.py"])], [], monkeypatch)
    assert got["surfaces"] == ["src/one.py"]
    assert not any(HEALED in n for n in got.get("lint") or [])
