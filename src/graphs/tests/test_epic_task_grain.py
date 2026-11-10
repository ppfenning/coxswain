"""A task starts when its own needs are merged; staleness asks for those commits, and the branch is cut from the parent's tip."""

from __future__ import annotations

import threading
from pathlib import Path

from harness.epic import _needs_missing, _open_phase_worktree, _open_view, _required_refs
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    cart,
    drive,
    git,
    is_ancestor,
    new_file_patch,
    repo,
)
from tests.test_epic_multi_parent import commit_file
from tests.test_epic_stale_phase_worktree import PHASE, _ctx


def stack(repo: Path, tmp_path: Path):  # noqa: F811
    """p1 holds a (merged) and slow (unmerged work in flight); returns ctx, p1 tree, a's commit, slow's commit."""
    ctx = _ctx(repo, tmp_path, "r1")
    tree = tmp_path / "parent-p1"
    git("worktree", "add", "-b", ctx.phase_branch("p1"), str(tree), "main", cwd=repo)
    commit_file(tree, "a.md", "a")
    sha_a = git("rev-parse", "HEAD", cwd=tree)
    commit_file(tree, "slow.md", "slow")
    return ctx, tree, sha_a, git("rev-parse", "HEAD", cwd=tree)


def items_for(sha_a: str, slow_state: str = "ready") -> list[dict]:
    return [
        {"id": "a", "phase": "p1", "state": "approved", "merged": True, "merged_commit": sha_a, "needs": []},
        {"id": "slow", "phase": "p1", "state": slow_state, "needs": []},
        {"id": "c", "phase": PHASE, "state": "ready", "needs": ["a"]},
        {"id": "d", "phase": PHASE, "state": "ready", "needs": ["a"]},
    ]


def test_a_task_with_merged_needs_opens_before_its_parent_phase_completes() -> None:
    view = _open_view(items_for("abc"), {"p1": set(), PHASE: {"p1"}}, set(), set())
    states = {i["id"]: i["state"] for i in view}
    assert states["c"] == "ready"
    held = [*items_for("abc"), {"id": "e", "phase": PHASE, "state": "ready", "needs": []}]
    assert {i["id"]: i["state"] for i in _open_view(held, {"p1": set(), PHASE: {"p1"}}, set(), set())}["e"] == "blocked"


def test_the_child_branch_is_cut_from_the_parents_tip_and_holds_its_other_work(repo, tmp_path) -> None:  # noqa: F811
    ctx, tree, sha_a, sha_slow = stack(repo, tmp_path)
    refs = _required_refs(items_for(sha_a), PHASE, ("p1",), ctx.phase_branch, "main", cut=True)
    assert refs == [ctx.phase_branch("p1")]
    ok, detail, _ = _open_phase_worktree(ctx, PHASE, "main", ("p1",), refs)
    assert ok, detail
    assert is_ancestor(repo, sha_a, ctx.phase_branch(PHASE))
    assert is_ancestor(repo, sha_slow, ctx.phase_branch(PHASE))


def test_a_parent_that_merges_unrelated_work_does_not_stale_the_child_branch(repo, tmp_path) -> None:  # noqa: F811
    ctx, tree, sha_a, _ = stack(repo, tmp_path)
    needed = _required_refs(items_for(sha_a), PHASE, ("p1",), ctx.phase_branch, "main")
    cut_from = _required_refs(items_for(sha_a), PHASE, ("p1",), ctx.phase_branch, "main", cut=True)
    ok, detail, _ = _open_phase_worktree(ctx, PHASE, "main", ("p1",), cut_from)
    assert ok, detail
    commit_file(tree, "unrelated.md", "unrelated")
    head = git("rev-parse", "HEAD", cwd=tree)
    # The old head check would call this branch stale.
    assert not is_ancestor(repo, head, ctx.phase_branch(PHASE))
    assert needed == [sha_a]
    assert _needs_missing(ctx, PHASE, needed) is False


def test_a_needed_commit_missing_from_the_child_branch_reads_as_stale(repo, tmp_path) -> None:  # noqa: F811
    ctx, tree, sha_a, _ = stack(repo, tmp_path)
    ok, detail, _ = _open_phase_worktree(ctx, PHASE, "main", ("p1",), [sha_a])
    assert ok, detail
    commit_file(tree, "b.md", "b")
    sha_b = git("rev-parse", "HEAD", cwd=tree)
    assert _needs_missing(ctx, PHASE, [sha_a]) is False
    assert _needs_missing(ctx, PHASE, [sha_a, sha_b]) is True
    assert _needs_missing(ctx, PHASE, [sha_b]) is True


TASKS = ("t1-probe", "t2-bench", "t3-cutover")


class HeldSlow(Runner):
    """`t2-bench` (slow) builds only once `t3-cutover` (c) has started, so c starts while slow is still building."""

    def __init__(self, repo_path: Path) -> None:
        super().__init__({t: new_file_patch(f"{t}.txt") for t in TASKS})
        self.repo, self.c_started, self.c_files = repo_path, threading.Event(), None

    def run(self, **kw):
        if kw["role"] == "build":
            task = self._subject(kw["prompt"], TASKS)
            if task == "t3-cutover":
                self.c_files = git(
                    "ls-tree", "-r", "--name-only", "epic/demo-initiative/p2-rollout", cwd=self.repo
                ).split()
                self.c_started.set()
            if task == "t2-bench":
                assert self.c_started.wait(20), "c never started while slow was building"
        return super().run(**kw)


def test_c_starts_while_slow_builds_and_slow_merging_later_leaves_its_branch_alone(repo, cart, tmp_path) -> None:  # noqa: F811
    spec = [
        ("t1-probe", "p1-foundations", []),
        ("t2-bench", "p1-foundations", ["t1-probe"]),
        ("t3-cutover", "p2-rollout", ["t1-probe"]),
    ]
    work = {
        "id": "demo-initiative", "title": "demo", "body": "goal", "phases": ["p1-foundations", "p2-rollout"],
        "items": [
            {"id": i, "phase": p, "state": "ready", "needs": n, "surfaces": [], "title": i, "body": f"do {i}"}
            for i, p, n in spec
        ],
    }
    runner = HeldSlow(repo)
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work)

    assert runner.c_files is not None
    assert "t1-probe.txt" in runner.c_files and "t2-bench.txt" not in runner.c_files
    p2 = next(p for p in result["phases"] if p["phase"] == "p2-rollout")
    assert all(t["merged"] for t in result["tasks"])
    assert p2["status"] != "blocked" and "recreated" not in p2


P1, P2 = "epic/demo-initiative/p1-foundations", "epic/demo-initiative/p2-rollout"


def first_run(repo, cart, tmp_path) -> dict:  # noqa: F811
    """Run the whole initiative once; return each p1 task's merge commit and an unrelated commit added on p1 afterwards."""
    drive(repo, cart, tmp_path, run_id="epic-1")
    merged = {
        t: git("log", "--merges", "-n1", "--format=%H", f"--grep=merge {t} into", P1, cwd=repo)
        for t in ("t1-probe", "t2-bench")
    }
    head = git("rev-parse", P1, cwd=repo)
    unrelated = git("commit-tree", head + "^{tree}", "-p", head, "-m", "unrelated merge", cwd=repo)
    git("update-ref", "refs/heads/" + P1, unrelated, cwd=repo)
    return {**merged, "unrelated": unrelated}


def restart(shas: dict, *, t1_commit: str | None = None, t3_state: str = "ready") -> dict:
    """The store a restart reads: p1's tasks approved and merged at their commits; t3 in `t3_state`."""
    from tests.test_epic_driver import initiative

    commits = {"t1-probe": t1_commit or shas["t1-probe"], "t2-bench": shas["t2-bench"]}
    items = [
        {**i, "state": "approved", "merged": True, "merged_commit": commits[i["id"]]}
        if i["id"] in commits
        else {**i, "state": t3_state, "merged": t3_state == "approved"}
        for i in initiative()["items"]
    ]
    return {**initiative(), "items": items}


def p2_record(result: dict) -> dict:
    return next(p for p in result["phases"] if p["phase"] == "p2-rollout")


def test_p2_is_reopened_on_its_branch_after_p1_moves_for_an_unrelated_merge(repo, cart, tmp_path) -> None:  # noqa: F811
    shas = first_run(repo, cart, tmp_path)
    # The old head check asked this question and would call the branch stale.
    assert not is_ancestor(repo, P1, P2)
    patches = {"t3-cutover": new_file_patch("t3-again.txt")}
    result, _ = drive(repo, cart, tmp_path, work=restart(shas), run_id="epic-2", patches=patches)
    p2 = p2_record(result)
    assert p2["reused_branch"] is True
    assert "recreated" not in p2
    assert p2["status"] != "blocked"


def test_a_needed_commit_missing_from_a_reused_branch_with_its_own_commits_blocks_it(repo, tmp_path) -> None:  # noqa: F811
    from harness.epic import _run_phase

    first = _ctx(repo, tmp_path, "r1")
    trees = {name: tmp_path / f"parent-{name}" for name in ("pa", "pb")}
    for name, tree in trees.items():
        git("worktree", "add", "-b", first.phase_branch(name), str(tree), "main", cwd=repo)
        commit_file(tree, f"{name}.md", name)
    shas = {name: git("rev-parse", "HEAD", cwd=tree) for name, tree in trees.items()}
    refs = [shas["pa"], shas["pb"]]
    ok, detail, _ = _open_phase_worktree(first, PHASE, "main", ("pa", "pb"), refs)
    assert ok, detail
    commit_file(first.phase_worktree(PHASE), "own.md", f"epic r1: merge t3-cutover into {PHASE}")
    # pb merges a second task after p2 was cut: p2 now lacks a commit it needs.
    commit_file(trees["pb"], "more.md", "more")
    newer = git("rev-parse", "HEAD", cwd=trees["pb"])
    items = [
        {"id": "ta", "phase": "pa", "state": "approved", "merged": True, "merged_commit": shas["pa"], "needs": []},
        {"id": "tb", "phase": "pb", "state": "approved", "merged": True, "merged_commit": newer, "needs": []},
        {"id": "t3-cutover", "phase": PHASE, "state": "approved", "merged": True, "needs": ["ta", "tb"]},
    ]
    record = _run_phase(_ctx(repo, tmp_path, "r2"), initiative={"id": "x"}, phase=PHASE, parents=("pa", "pb"), items=items)
    assert record["reused_branch"] is True
    assert record["status"] == "blocked"
    assert "recreated" not in record
    assert not is_ancestor(repo, newer, first.phase_branch(PHASE))


def test_a_needed_commit_missing_from_a_reused_branch_with_nothing_of_its_own_recreates_it(repo, cart, tmp_path) -> None:  # noqa: F811
    shas = first_run(repo, cart, tmp_path)
    # p1's task commits land on main with the same patches, so only t3's commit is unlanded on p2 and it is ready again.
    for t in ("t1-probe", "t2-bench"):
        task_commit = git("log", "--no-merges", "-n1", "--format=%H", f"--grep=^epic epic-1: {t}$", P1, cwd=repo)
        git("cherry-pick", task_commit, cwd=repo)
    # t3 needs t1, and t1's merged_commit is a commit p2's branch has never held.
    work = restart(shas, t1_commit=shas["unrelated"])
    result, _ = drive(
        repo, cart, tmp_path, work=work, run_id="epic-2", patches={"t3-cutover": new_file_patch("t3-again.txt")}
    )
    p2 = p2_record(result)
    assert "recreated" in p2, {k: p2.get(k) for k in ("status", "reason", "reused_branch", "rebased", "quarantined")}
    assert is_ancestor(repo, shas["unrelated"], P2)


def write_commit(tree: Path, name: str, text: str) -> str:
    (tree / name).write_text(text)
    git("add", name, cwd=tree)
    git("commit", "-m", f"{name}: {text}", cwd=tree)
    return git("rev-parse", "HEAD", cwd=tree)


def test_a_conflicting_assembly_holds_the_starting_task_and_leaves_its_sibling(repo, tmp_path) -> None:  # noqa: F811
    from harness.epic import ASSEMBLY_FAILED, _run_phase

    ctx = _ctx(repo, tmp_path, "r1")
    shas = {}
    for name, text in (("pa", "left"), ("pb", "right")):
        tree = tmp_path / f"parent-{name}"
        git("worktree", "add", "-b", ctx.phase_branch(name), str(tree), "main", cwd=repo)
        shas[name] = write_commit(tree, "x.md", text)
    items = [
        {"id": "ta", "phase": "pa", "state": "approved", "merged": True, "merged_commit": shas["pa"], "needs": []},
        {"id": "tb", "phase": "pb", "state": "approved", "merged": True, "merged_commit": shas["pb"], "needs": []},
        {"id": "c", "phase": PHASE, "state": "ready", "needs": ["ta", "tb"]},
        {"id": "sibling", "phase": PHASE, "state": "ready", "needs": []},
    ]
    record = _run_phase(
        ctx, initiative={"id": "x"}, phase=PHASE, parents=("pa", "pb"), items=items, only=frozenset({"c"})
    )
    assert record["status"] == "blocked"
    assert record["reason"].startswith(ASSEMBLY_FAILED) and "conflicted in: x.md" in record["reason"]
    assert [(q["id"], q["grain"]) for q in record["quarantined"]] == [("c", "task")]
    assert next(i for i in items if i["id"] == "sibling")["state"] == "ready"
