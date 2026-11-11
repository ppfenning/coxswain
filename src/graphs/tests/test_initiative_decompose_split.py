"""decompose moves a task whose files live in another registered repository into a follow-up."""

from __future__ import annotations

import subprocess

import pytest

from graphs._contract import ContractViolation
from graphs.delivery import initiative_decompose
from runner import ScriptedRunner

OWN = [{"repo": "me", "path": "src/a.py"}, {"repo": "me", "path": "src/c.py"}]
TREES = {"/repos/other": ["lib/b.py"]}


def _task(tid: str, surfaces: list[str], body: str) -> dict:
    return {"id": tid, "phase": "p1", "title": tid, "body": body, "needs": [], "surfaces": surfaces}


def _plan(*tasks: dict) -> dict:
    return {"phases": [{"id": "p1", "goal": "g"}], "tasks": list(tasks), "rationale": "r"}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["decompose"] = "acme-skills:decompose"
    cartridge["work_routing"] = {"states": {"active": "work", "planned": "work", "future": "backlog"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "deferred"}
    cartridge["write_kinds"]["state_move"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


def decompose(cart, plan: dict, **extra) -> dict:
    args = {"run_id": "r", "date": "2026-11-06", "cartridge": cart, "idea": "# Split\n\ngo", "tree": OWN, **extra}
    return initiative_decompose.run(args, ScriptedRunner({"decompose": plan}))


def test_a_task_in_another_repository_becomes_a_follow_up(cart) -> None:
    plan = _plan(_task("t1", ["src/a.py"], "edit a"), _task("t2", ["lib/b.py"], "edit b in the other repo"))
    result = decompose(cart, plan, repo_trees=TREES)
    assert [t["id"] for t in result["tasks"]] == ["t1"]
    assert len(result["follow_ups"]) == 1
    follow_up = result["follow_ups"][0]
    assert follow_up["repo"] == "/repos/other"
    assert "edit b in the other repo" in follow_up["body"]
    assert follow_up["tasks"] == ["t2"]
    assert result["dropped"] == []


def test_the_split_is_recorded_in_initiative_md_and_proposes_only_kept_tasks(cart) -> None:
    plan = _plan(_task("t1", ["src/a.py"], "edit a"), _task("t2", ["lib/b.py"], "edit b"))
    result = decompose(cart, plan, repo_trees=TREES)
    targets = [p["target"] for p in result["proposals"]]
    assert targets == ["t1", "initiative"]
    initiative = next(p for p in result["proposals"] if p["target"] == "initiative")
    assert "Task t2 has no surface here" in initiative["suggested_action"]


def test_a_task_in_no_known_repository_is_dropped(cart) -> None:
    plan = _plan(_task("t1", ["src/a.py"], "edit a"), _task("t2", ["nowhere/x.py"], "edit x"))
    result = decompose(cart, plan, repo_trees=TREES)
    assert [t["id"] for t in result["tasks"]] == ["t1"]
    assert [d["task"]["id"] for d in result["dropped"]] == ["t2"]
    assert result["follow_ups"] == []


def test_an_empty_repo_trees_changes_nothing(cart) -> None:
    plan = _plan(_task("t1", ["src/a.py"], "edit a"), _task("t2", ["src/c.py"], "edit c"))
    without = decompose(cart, plan)
    for extra in ({"repo_trees": {}}, {"repo_trees": None}):
        result = decompose(cart, plan, **extra)
        assert result["tasks"] == without["tasks"]
        assert result["proposals"] == without["proposals"]
        assert (result["follow_ups"], result["dropped"]) == ([], [])
    assert (without["follow_ups"], without["dropped"]) == ([], [])


def test_an_all_foreign_plan_is_refused_naming_the_owner(cart) -> None:
    plan = _plan(_task("t1", ["lib/b.py"], "edit b"))
    with pytest.raises(ContractViolation, match="/repos/other"):
        decompose(cart, plan, repo_trees=TREES)


def _phase_plan(*tasks: dict) -> dict:
    return {"phases": [{"id": "p1", "goal": "g"}, {"id": "p2", "goal": "h"}], "tasks": list(tasks), "rationale": "r"}


def test_a_phase_left_with_no_task_is_removed(cart) -> None:
    moved = {**_task("t2", ["lib/b.py"], "edit b"), "phase": "p2"}
    result = decompose(cart, _phase_plan(_task("t1", ["src/a.py"], "edit a"), moved), repo_trees=TREES)
    assert [p["id"] for p in result["phases"]] == ["p1"]
    assert result["totals"]["phases"] == 1


def test_a_basename_or_directory_surface_of_this_repository_is_kept(cart) -> None:
    plan = _plan(_task("t1", ["a.py"], "edit a"), _task("t2", ["src/"], "edit src"), _task("t3", ["lib/b.py"], "b"))
    result = decompose(cart, plan, repo_trees=TREES)
    assert sorted(t["id"] for t in result["tasks"]) == ["t1", "t2"]
    assert result["dropped"] == []


def test_a_stack_sibling_path_stays_with_the_stack(cart) -> None:
    tree = [*OWN, {"repo": "sibling", "path": "pkg/s.py"}]
    plan = _plan(_task("t1", ["src/a.py"], "edit a"), _task("t2", ["pkg/s.py"], "edit s"))
    result = decompose(cart, plan, tree=tree, repo_trees={"/repos/other": ["lib/b.py", "pkg/s.py"]})
    assert result["follow_ups"] == []
    assert result["dropped"] == []
    assert len(result["tasks"]) == 2


def test_a_kept_task_loses_its_edge_to_a_moved_task_and_the_note_is_recorded(cart) -> None:
    kept = {**_task("t2", ["src/a.py"], "edit a"), "needs": ["t1"]}
    result = decompose(cart, _plan(_task("t1", ["lib/b.py"], "edit b"), kept), repo_trees=TREES)
    assert result["tasks"][0]["needs"] == []
    initiative = next(p for p in result["proposals"] if p["target"] == "initiative")
    assert "Removed edge: task t2 no longer needs t1" in initiative["suggested_action"]


def test_a_mixed_task_keeps_its_own_surface_and_moves_the_rest(cart) -> None:
    result = decompose(cart, _plan(_task("t1", ["src/a.py", "lib/b.py"], "edit both")), repo_trees=TREES)
    assert result["tasks"][0]["surfaces"] == ["src/a.py"]
    assert result["follow_ups"][0]["tasks"] == ["t1--other"]


def test_the_checkout_listing_supplies_the_own_paths_without_a_tree(cart, tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    plan = _plan(_task("t1", ["src/a.py"], "edit a"), _task("t2", ["lib/b.py"], "edit b"))
    result = decompose(cart, plan, tree=[], checkout=str(tmp_path), repo_trees=TREES)
    assert [t["id"] for t in result["tasks"]] == ["t1"]
    assert result["follow_ups"][0]["tasks"] == ["t2"]


def test_a_rewritten_plan_is_split_again_and_replaces_the_first_split(cart) -> None:
    first = _plan(_task("t1", ["src/a.py"], "```bash\nruff check .\n```"), _task("t9", ["lib/b.py"], "old foreign"))
    second = _plan(_task("t1", ["src/a.py"], "```bash\npytest -q\n```"), _task("t2", ["lib/b.py"], "new foreign"))
    runner = ScriptedRunner({"decompose": [first, second]})
    args = {"run_id": "r", "date": "d", "cartridge": cart, "idea": "# S\n\ngo", "tree": OWN, "grants": ["pytest"]}
    result = initiative_decompose.run({**args, "repo_trees": TREES}, runner)
    assert [c["role"] for c in runner.calls] == ["decompose", "decompose"]
    assert [t["id"] for t in result["tasks"]] == ["t1"]
    assert [f["tasks"] for f in result["follow_ups"]] == [["t2"]]


COX = "~/repos/coxswain"
DASH = "~/repos/coxswain-dash"
COX_OWN = [{"repo": "me", "path": "src/tools/agent_tools/a.py"}]
DASH_OWN = [{"repo": "me", "path": "src/ui/regatta.rs"}]


def test_a_layout_foreign_task_becomes_a_follow_up_without_repo_trees(cart) -> None:
    plan = _plan(
        _task("t1", ["src/tools/agent_tools/a.py"], "edit a"),
        _task("t2", ["src/ui/regatta.rs", "Cargo.toml"], "edit the dashboard"),
    )
    result = decompose(cart, plan, tree=COX_OWN, repo=COX)
    assert [t["id"] for t in result["tasks"]] == ["t1"]
    assert [(f["repo"], f["tasks"]) for f in result["follow_ups"]] == [(DASH, ["t2"])]
    assert result["dropped"] == []


def test_a_coxswain_dash_initiative_moves_a_coxswain_task_by_layout(cart) -> None:
    plan = _plan(
        _task("t1", ["src/ui/regatta.rs"], "edit regatta"),
        _task("t2", ["src/tools/agent_tools/dash_feed.py"], "edit the feed"),
    )
    result = decompose(cart, plan, tree=DASH_OWN, repo=DASH)
    assert [t["id"] for t in result["tasks"]] == ["t1"]
    assert [f["repo"] for f in result["follow_ups"]] == [COX]


def test_a_plan_wholly_in_the_layout_repository_is_refused_naming_it(cart) -> None:
    plan = _plan(_task("t1", ["src/ui/regatta.rs"], "edit regatta"))
    with pytest.raises(ContractViolation, match=f"every task in this plan belongs to {DASH}"):
        decompose(cart, plan, tree=COX_OWN, repo=COX)


def test_a_plan_with_no_known_own_key_is_unchanged(cart) -> None:
    plan = _plan(_task("t1", ["src/a.py"], "edit a"), _task("t2", ["src/ui/regatta.rs"], "edit regatta"))
    with pytest.raises(ContractViolation, match="t2: src/ui/regatta.rs: not in the checkout"):
        decompose(cart, plan, repo="repo/u")
    kept = decompose(cart, _plan(_task("t1", ["src/a.py"], "edit a")), repo="repo/u")
    assert [t["id"] for t in kept["tasks"]] == ["t1"]
    assert kept["follow_ups"] == []
