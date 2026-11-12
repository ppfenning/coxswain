"""Real-repo tests of the landing model's git: the initiative branch, the phase merge and the guard."""
import subprocess
from pathlib import Path

import pytest

from harness import landing_git as lg

ENV_ID = ("-c", "user.email=t@invalid", "-c", "user.name=t")


def run(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *ENV_ID, *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(repo: Path, branch: str, name: str, text: str) -> str:
    """One commit on `branch` writing `name`, made on a scratch worktree so the main checkout never moves."""
    if run(repo, "branch", "--show-current") == branch:  # the main checkout's own branch: commit in place
        (repo / name).write_text(text)
        run(repo, "add", "--all")
        run(repo, "commit", "-m", f"{branch}: {name}")
        return run(repo, "rev-parse", "HEAD")
    wt = repo.parent / f"wt-{branch.replace('/', '_')}"
    run(repo, "worktree", "add", str(wt), branch)
    try:
        (wt / name).parent.mkdir(parents=True, exist_ok=True)
        (wt / name).write_text(text)
        run(wt, "add", "--all")
        run(wt, "commit", "-m", f"{branch}: {name}")
        return run(wt, "rev-parse", "HEAD")
    finally:
        run(repo, "worktree", "remove", "--force", str(wt))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    run(path, "init", "-b", "main")
    (path / "base.txt").write_text("base\n")
    (path / "shared.txt").write_text("one\ntwo\nthree\n")
    run(path, "add", "--all")
    run(path, "commit", "-m", "base")
    return path


def cut(repo: Path, branch: str, at: str) -> None:
    run(repo, "branch", branch, at)


def test_the_initiative_branch_is_cut_once_from_the_parent_and_never_moved_by_a_second_call(repo):
    base = run(repo, "rev-parse", "main")
    assert lg.ensure_branch(repo, "init/i", "main") == (base, True, "")
    commit(repo, "main", "later.txt", "later\n")
    assert lg.ensure_branch(repo, "init/i", "main") == (base, False, ""), "an existing branch keeps its tip"
    assert lg.ensure_branch(repo, "init/j", "no-such-ref")[0] is None


def test_a_two_wave_initiative_the_wave_two_base_contains_wave_one(repo):
    lg.ensure_branch(repo, "init/i", "main")
    cut(repo, "epic/i/p1", "init/i")
    p1_tip = commit(repo, "epic/i/p1", "p1.txt", "p1\n")
    merged = lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge phase p1")
    assert merged.status == "merged" and merged.merged == p1_tip
    assert run(repo, "rev-parse", "init/i") == merged.tip
    assert run(repo, "rev-list", "--parents", "-n", "1", merged.tip).split()[1:] == [run(repo, "rev-parse", "main"), p1_tip]
    cut(repo, "epic/i/p2", "init/i")  # wave 2 is cut from the initiative tip, after its parent merged
    assert lg.is_ancestor(repo, p1_tip, "epic/i/p2")
    assert run(repo, "show", "epic/i/p2:p1.txt") == "p1"


def test_two_sibling_phases_merge_in_either_order(repo):
    for order in (("p1", "p2"), ("p2", "p1")):
        branch = f"init/{'-'.join(order)}"
        lg.ensure_branch(repo, branch, "main")
        tips = {}
        for phase in ("p1", "p2"):
            name = f"epic/{'-'.join(order)}/{phase}"
            cut(repo, name, branch)
            tips[phase] = commit(repo, name, f"{phase}.txt", f"{phase}\n")
        results = [lg.merge_into(repo, branch=branch, source=f"epic/{'-'.join(order)}/{p}", message=f"merge {p}") for p in order]
        assert [r.status for r in results] == ["merged", "merged"]
        assert all(lg.is_ancestor(repo, tips[p], branch) for p in ("p1", "p2"))
        assert run(repo, "ls-tree", "--name-only", branch).split() == ["base.txt", "p1.txt", "p2.txt", "shared.txt"]


def test_a_phase_already_on_the_branch_is_up_to_date_and_moves_nothing(repo):
    lg.ensure_branch(repo, "init/i", "main")
    cut(repo, "epic/i/p1", "init/i")
    commit(repo, "epic/i/p1", "p1.txt", "p1\n")
    first = lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1")
    again = lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1")
    assert (again.status, again.tip) == ("up_to_date", first.tip)


def test_a_conflict_aborts_names_the_paths_and_leaves_the_branch_and_the_checkout_alone(repo):
    lg.ensure_branch(repo, "init/i", "main")
    cut(repo, "epic/i/p1", "init/i")
    cut(repo, "epic/i/p2", "init/i")
    commit(repo, "epic/i/p1", "shared.txt", "one\nTWO from p1\nthree\n")
    commit(repo, "epic/i/p2", "shared.txt", "one\nTWO from p2\nthree\n")
    assert lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1").ok
    before = run(repo, "rev-parse", "init/i")
    clash = lg.merge_into(repo, branch="init/i", source="epic/i/p2", message="merge p2")
    assert (clash.status, clash.paths, clash.tip) == ("conflict", ("shared.txt",), before)
    assert run(repo, "rev-parse", "init/i") == before
    assert run(repo, "status", "--porcelain") == ""
    assert "lander-" not in run(repo, "worktree", "list")


def test_failing_checks_leave_the_branch_where_it_was_and_passing_checks_see_the_merged_tree(repo):
    lg.ensure_branch(repo, "init/i", "main")
    cut(repo, "epic/i/p1", "init/i")
    commit(repo, "epic/i/p1", "p1.txt", "p1\n")
    before = run(repo, "rev-parse", "init/i")
    seen = []

    def failing(tree: Path) -> tuple[bool, str]:
        seen.append(sorted(p.name for p in tree.iterdir() if p.name != ".git"))
        return False, "1 failed"

    red = lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1", checks=failing)
    assert (red.status, red.detail, red.tip) == ("checks_failed", "1 failed", before)
    assert seen == [["base.txt", "p1.txt", "shared.txt"]]
    assert run(repo, "rev-parse", "init/i") == before
    green = lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1", checks=lambda tree: (True, "ok"))
    assert green.status == "merged"


def test_a_merge_that_would_drop_a_recorded_commit_is_refused_and_the_branch_is_unchanged(repo):
    lg.ensure_branch(repo, "init/i", "main")
    cut(repo, "epic/i/p1", "init/i")
    commit(repo, "epic/i/p1", "p1.txt", "p1\n")
    before = run(repo, "rev-parse", "init/i")
    lost = commit(repo, "main", "elsewhere.txt", "x\n")  # a sha the record names that is not on this branch
    refused = lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1", recorded=[lost])
    assert refused.status == "guard" and lost[:12] in refused.detail
    assert run(repo, "rev-parse", "init/i") == before
    assert lg.missing_from(repo, "init/i", [before, lost]) == [lost]


def test_refreshing_onto_an_advanced_parent_is_a_forward_merge_that_keeps_every_phase_merge(repo):
    lg.ensure_branch(repo, "init/i", "main")
    cut(repo, "epic/i/p1", "init/i")
    p1_tip = commit(repo, "epic/i/p1", "p1.txt", "p1\n")
    merge = lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1")
    moved = commit(repo, "main", "main-moved.txt", "moved\n")
    refreshed = lg.merge_into(repo, branch="init/i", source="main", message="merge main", recorded=[p1_tip, merge.tip])
    assert refreshed.status == "merged"
    assert all(lg.is_ancestor(repo, sha, "init/i") for sha in (p1_tip, merge.tip, moved))
    assert lg.ahead_behind(repo, "init/i", "main") == (3, 0)


def test_a_conflict_commit_puts_the_markers_on_the_phase_branch_so_a_rectify_task_can_resolve_them(repo):
    lg.ensure_branch(repo, "init/i", "main")
    cut(repo, "epic/i/p1", "init/i")
    cut(repo, "epic/i/p2", "init/i")
    commit(repo, "epic/i/p1", "shared.txt", "one\nTWO from p1\nthree\n")
    commit(repo, "epic/i/p2", "shared.txt", "one\nTWO from p2\nthree\n")
    lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1")
    marked = lg.conflict_commit(repo, branch="epic/i/p2", source="init/i", message="merge init/i into p2 (conflicted)")
    assert (marked.status, marked.paths) == ("conflict", ("shared.txt",))
    assert "<<<<<<<" in run(repo, "show", "epic/i/p2:shared.txt")
    assert lg.is_ancestor(repo, "init/i", "epic/i/p2"), "the phase now holds the initiative tip"
    commit(repo, "epic/i/p2", "shared.txt", "one\nTWO from both\nthree\n")  # what the rectify task's patch does
    healed = lg.merge_into(repo, branch="init/i", source="epic/i/p2", message="merge p2")
    assert healed.status == "merged"
    assert run(repo, "show", "init/i:shared.txt") == "one\nTWO from both\nthree"


def test_a_branch_holding_the_only_copy_of_a_recorded_commit_is_not_safe_to_delete(repo):
    lg.ensure_branch(repo, "init/i", "main")
    cut(repo, "epic/i/p1", "init/i")
    p1_tip = commit(repo, "epic/i/p1", "p1.txt", "p1\n")
    assert lg.safe_to_delete(repo, "epic/i/p1", run(repo, "rev-parse", "init/i"), [p1_tip]) == [p1_tip]
    merged = lg.merge_into(repo, branch="init/i", source="epic/i/p1", message="merge p1")
    assert lg.safe_to_delete(repo, "epic/i/p1", merged.tip, [p1_tip]) == []
