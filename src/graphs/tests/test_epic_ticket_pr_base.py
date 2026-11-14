"""At land_at ticket, a phase that builds on an earlier phase is cut from pr/<initiative> once that branch exists."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from harness.epic import _open_phase_worktree, _run_phase
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    cart,
    drive,
    git,
    initiative,
    is_ancestor,
    repo,
)
from tests.test_epic_land_at import _advance_main
from tests.test_epic_multi_parent import commit_file, phase_record, two_parent_work
from tests.test_epic_stale_phase_worktree import PHASE, _ctx

PR = "pr/demo-initiative"
JOIN = "epic/demo-initiative/p2-rollout"
P1_TASKS = ("t1-probe", "t2-bench")


def make_pr_branch(root: Path, tmp_path: Path, *files: str) -> None:
    """pr/demo-initiative: main plus one commit per file, as a land of phase one would leave it."""
    tree = tmp_path / "pr-tree"
    git("worktree", "add", "-b", PR, str(tree), "main", cwd=root)
    for name in files:
        commit_file(tree, name, f"epic r0: {name}")
    git("worktree", "remove", "--force", str(tree), cwd=root)


def with_landed(work: dict, *tasks: str) -> dict:
    """Phase one's tasks approved and merged into the pr branch: the parent edge stays, the build is not repeated."""
    items = [{**i, "state": "approved", "merged": True} if i["id"] in tasks else i for i in work["items"]]
    return {**work, "items": items}


def with_done(work: dict, *tasks: str) -> dict:
    """Phase one's tasks done, as a land leaves them: `phase_parents` then drops the parent edge."""
    return {**work, "items": [{**i, "state": "done"} if i["id"] in tasks else i for i in work["items"]]}


def test_phase_two_is_cut_from_the_pr_branch_and_holds_phase_ones_file(repo, cart, tmp_path) -> None:  # noqa: F811
    make_pr_branch(repo, tmp_path, "t1-probe.txt")
    result, _ = drive(repo, cart, tmp_path, work=with_landed(initiative(), *P1_TASKS))

    second = phase_record(result, "p2-rollout")
    assert second["base"] == PR
    assert second["status"] != "blocked"
    assert is_ancestor(repo, PR, JOIN)
    assert git("show", f"{JOIN}:t1-probe.txt", cwd=repo) == "t1-probe.txt"


def test_phase_two_is_cut_from_the_pr_branch_once_phase_one_is_done(repo, cart, tmp_path) -> None:  # noqa: F811
    make_pr_branch(repo, tmp_path, "t1-probe.txt")
    result, _ = drive(repo, cart, tmp_path, work=with_done(initiative(), *P1_TASKS))

    second = phase_record(result, "p2-rollout")
    assert second["base"] == PR
    assert second["status"] != "blocked"
    assert git("show", f"{JOIN}:t1-probe.txt", cwd=repo) == "t1-probe.txt"


def test_phase_two_builds_on_the_pr_branch_after_phase_one_ran_and_main_moved(repo, cart, tmp_path) -> None:  # noqa: F811
    """The composed state: a phase-one branch exists, main has moved, and the land put phase one in the pr branch."""
    drive(repo, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-1")
    _advance_main(repo)
    make_pr_branch(repo, tmp_path, "t1-probe.txt", "t2-bench.txt")
    result, runner = drive(repo, cart, tmp_path, work=with_done(initiative(), *P1_TASKS), run_id="epic-2")

    second = phase_record(result, "p2-rollout")
    assert second["base"] == PR
    assert second["status"] != "blocked"
    assert any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)
    assert git("show", f"{JOIN}:t1-probe.txt", cwd=repo) == "t1-probe.txt"
    assert is_ancestor(repo, PR, JOIN)


def test_a_phase_with_two_parents_is_cut_from_the_pr_branch_without_assembly(repo, cart, tmp_path) -> None:  # noqa: F811
    make_pr_branch(repo, tmp_path, "t1-probe.txt", "t2-bench.txt")
    result, _ = drive(repo, cart, tmp_path, work=with_landed(two_parent_work(), *P1_TASKS))

    join = phase_record(result, "p2-rollout")
    assert join["base"] == PR
    assert "assembled_from" not in join
    assert join["status"] != "blocked"
    assert is_ancestor(repo, PR, JOIN)
    assert git("show", f"{JOIN}:t2-bench.txt", cwd=repo) == "t2-bench.txt"


def test_without_a_pr_branch_every_phase_is_cut_from_the_initiative_branch(repo, cart, tmp_path) -> None:  # noqa: F811
    main = git("rev-parse", "main", cwd=repo)
    result, _ = drive(repo, cart, tmp_path)

    assert phase_record(result, "p1-foundations")["base"] == "init/demo-initiative"
    assert phase_record(result, "p2-rollout")["base"] == "init/demo-initiative"
    assert is_ancestor(repo, main, "init/demo-initiative")


def test_a_reused_branch_is_rebased_when_the_pr_branch_moves(repo, cart, tmp_path) -> None:  # noqa: F811
    make_pr_branch(repo, tmp_path, "t1-probe.txt")
    first = _ctx(repo, tmp_path, "r1")
    ok, detail, _ = _open_phase_worktree(first, PHASE, PR, ("p1-foundations",), [PR])
    assert ok, detail
    tree = first.phase_worktree(PHASE)
    commit_file(tree, "done.md", "epic r1: t-done")
    commit_file(tree, "waiting.md", "epic r1: t3-cutover")
    git("checkout", "--detach", "-q", cwd=tree)
    moved = tmp_path / "pr-moved"
    git("worktree", "add", str(moved), PR, cwd=repo)
    commit_file(moved, "later.md", "epic r0: later")
    git("worktree", "remove", "--force", str(moved), cwd=repo)

    items = [
        {"id": "t-done", "phase": PHASE, "state": "done", "needs": [], "surfaces": []},
        {"id": "t3-cutover", "phase": PHASE, "state": "approved", "needs": [], "surfaces": []},
    ]
    ctx = replace(_ctx(repo, tmp_path, "r2"), cartridge=cart)
    record = _run_phase(ctx, initiative={"id": "x"}, phase=PHASE, parents=("p1-foundations",), items=items)

    assert record["base"] == PR
    assert record["rebased"] is True
    assert record["status"] != "blocked"
    assert is_ancestor(repo, PR, first.phase_branch(PHASE))
