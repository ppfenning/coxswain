"""A level with several parent levels waits for all of them, then builds on a merge of their branches."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.epic import ASSEMBLY_FAILED, _needs_missing, _open_phase_worktree, _required_refs, _run_phase
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    cart,
    drive,
    git,
    initiative,
    is_ancestor,
    new_file_patch,
    repo,
)
from tests.test_epic_stale_phase_worktree import PHASE, _ctx

LEFT = "epic/demo-initiative/p1-foundations"
RIGHT = "epic/demo-initiative/p1-bench"
JOIN = "epic/demo-initiative/p2-rollout"


def two_parent_work() -> dict:
    """t3 in p2-rollout needs t1 (p1-foundations) and t2 (p1-bench)."""
    work = initiative()
    work["items"][1]["phase"] = "p1-bench"
    work["items"][2]["needs"] = ["t1-probe", "t2-bench"]
    work["phases"] = sorted({i["phase"] for i in work["items"]})
    return work


def readme_patch(text: str) -> str:
    return (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-# demo\n"
        f"+{text}\n"
    )


def commit_file(tree: Path, name: str, subject: str) -> None:
    (tree / name).write_text(name, encoding="utf-8")
    git("add", "-A", cwd=tree)
    git("commit", "-qm", subject, cwd=tree)


def phase_record(result: dict, phase: str) -> dict:
    return next(p for p in result["phases"] if p["phase"] == phase)


@pytest.mark.usefixtures("at_task")
def test_a_level_with_two_parents_builds_on_a_merge_of_both(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, work=two_parent_work())

    join = phase_record(result, "p2-rollout")
    assert join["status"] == "complete"
    assert join["assembled_from"] == ["p1-bench", "p1-foundations"]
    assert is_ancestor(repo, LEFT, JOIN)
    assert is_ancestor(repo, RIGHT, JOIN)
    assert git("show", f"{JOIN}:t1-probe.txt", cwd=repo) == "ok"
    assert git("show", f"{JOIN}:t2-bench.txt", cwd=repo) == "ok"
    merges = git("log", "--merges", "--format=%s", f"main..{JOIN}", cwd=repo)
    assert RIGHT in merges
    assert LEFT in merges


def test_a_level_with_two_parents_waits_until_both_are_complete(repo, cart, tmp_path) -> None:  # noqa: F811
    patches = {"t1-probe": new_file_patch("t1-probe.txt")}
    result, _ = drive(repo, cart, tmp_path, work=two_parent_work(), patches=patches)

    waiting = phase_record(result, "p2-rollout")
    assert waiting["status"] == "blocked"
    assert "parent phase 'p1-bench' did not meet its goal" in waiting["reason"]
    assert JOIN not in git("branch", "--format=%(refname:short)", cwd=repo).splitlines()


@pytest.mark.usefixtures("at_task")
def test_a_conflicting_pair_is_blocked_with_the_assembly_cause(repo, cart, tmp_path) -> None:  # noqa: F811
    patches = {
        "t1-probe": readme_patch("# one"),
        "t2-bench": readme_patch("# two"),
        "t3-cutover": new_file_patch("t3-cutover.txt"),
    }
    work = two_parent_work()
    result, _ = drive(repo, cart, tmp_path, work=work, patches=patches)

    left, right = (f"epic/{work['id']}/{work['items'][i]['phase']}" for i in (0, 1))
    join = phase_record(result, "p2-rollout")
    assert join["status"] == "blocked"
    assert join["reason"].startswith(f"{ASSEMBLY_FAILED}merging {left} after {right} conflicted in: README.md")
    assert "phase_failed_to_start" not in join
    entries = [q for q in result["quarantined"] if q["phase"] == "p2-rollout"]
    assert [(q["id"], q["grain"]) for q in entries] == [("t3-cutover", "task")]
    assert "conflicted in: README.md" in entries[0]["reason"]
    assert JOIN not in git("branch", "--format=%(refname:short)", cwd=repo).splitlines()


@pytest.mark.usefixtures("at_task")
def test_a_one_parent_chain_is_unchanged(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path)

    second = phase_record(result, "p2-rollout")
    assert second["status"] == "complete"
    assert second["parent"] == "p1-foundations"
    assert second["base"] == LEFT
    assert "assembled_from" not in second
    assert is_ancestor(repo, LEFT, JOIN)
    merges = git("log", "--merges", "--format=%s", f"{LEFT}..{JOIN}", cwd=repo).splitlines()
    assert merges == ["epic epic-1: merge t3-cutover into p2-rollout"]


def test_the_needs_check_covers_every_parent(repo, tmp_path) -> None:  # noqa: F811
    ctx = _ctx(repo, tmp_path, "r1")
    parents = ("pa", "pb")
    trees = {name: tmp_path / f"parent-{name}" for name in parents}
    for name, tree in trees.items():
        git("worktree", "add", "-b", ctx.phase_branch(name), str(tree), "main", cwd=repo)
        commit_file(tree, f"{name}.md", name)
    items = [
        {"id": f"t-{name}", "phase": name, "state": "approved", "merged": True, "needs": [],
         "merged_commit": git("rev-parse", "HEAD", cwd=tree)}
        for name, tree in trees.items()
    ] + [{"id": "t3-cutover", "phase": PHASE, "state": "ready", "needs": ["t-pa", "t-pb"]}]
    refs = _required_refs(items, PHASE, parents, ctx.phase_branch, "main")
    assert sorted(refs) == sorted(i["merged_commit"] for i in items[:2])
    ok, detail, _ = _open_phase_worktree(ctx, PHASE, "main", parents, refs)
    assert ok, detail
    assert _needs_missing(ctx, PHASE, refs) is False
    commit_file(trees["pb"], "more.md", "more")
    assert _needs_missing(ctx, PHASE, refs) is False
    stale = [*refs, git("rev-parse", "HEAD", cwd=trees["pb"])]
    assert _needs_missing(ctx, PHASE, stale) is True


def test_a_cut_uses_each_parents_branch_tip_while_the_needs_check_keeps_the_stored_commits(repo, tmp_path) -> None:  # noqa: F811
    ctx = _ctx(repo, tmp_path, "r1")
    parents = ("pa", "pb")
    trees = {name: tmp_path / f"parent-{name}" for name in parents}
    for name, tree in trees.items():
        git("worktree", "add", "-b", ctx.phase_branch(name), str(tree), "main", cwd=repo)
        commit_file(tree, f"{name}.md", name)
    stored = {name: git("rev-parse", "HEAD", cwd=tree) for name, tree in trees.items()}
    commit_file(trees["pb"], "later.md", "later")
    later = git("rev-parse", "HEAD", cwd=trees["pb"])
    items = [
        {"id": f"t-{name}", "phase": name, "state": "approved", "merged": True, "needs": [], "merged_commit": stored[name]}
        for name in parents
    ] + [{"id": "t3-cutover", "phase": PHASE, "state": "ready", "needs": ["t-pa", "t-pb"]}]

    cut = _required_refs(items, PHASE, parents, ctx.phase_branch, "main", cut=True)
    assert cut == [ctx.phase_branch("pa"), ctx.phase_branch("pb")]
    assert _required_refs(items, PHASE, parents, ctx.phase_branch, "main") == sorted(stored.values())
    ok, detail, _ = _open_phase_worktree(ctx, PHASE, "main", parents, cut)
    assert ok, detail
    assert is_ancestor(repo, later, ctx.phase_branch(PHASE))


def test_a_reused_two_parent_branch_with_own_commits_is_blocked_not_rebased(repo, tmp_path) -> None:  # noqa: F811
    first = _ctx(repo, tmp_path, "r1")
    parents = ("pa", "pb")
    trees = {name: tmp_path / f"parent-{name}" for name in parents}
    for name, tree in trees.items():
        git("worktree", "add", "-b", first.phase_branch(name), str(tree), "main", cwd=repo)
        commit_file(tree, f"{name}.md", name)
    ok, detail, _ = _open_phase_worktree(first, PHASE, "main", parents)
    assert ok, detail
    commit_file(first.phase_worktree(PHASE), "own.md", f"epic r1: merge t3-cutover into {PHASE}")
    commit_file(trees["pb"], "more.md", "more")

    items = [{"id": "t3-cutover", "phase": PHASE, "state": "approved", "needs": [], "surfaces": []}]
    record = _run_phase(_ctx(repo, tmp_path, "r2"), initiative={"id": "x"}, phase=PHASE, parents=parents, items=items)

    assert record["status"] == "blocked"
    assert record["rebased"] is False
    assert "one of which has moved since" in record["reason"]
    assert [q["grain"] for q in record["quarantined"]] == ["phase"]
    assert not is_ancestor(repo, first.phase_branch("pb"), first.phase_branch(PHASE))
