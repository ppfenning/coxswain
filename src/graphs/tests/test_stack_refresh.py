"""Stack refresh merges the default ref into a level branch and leaves a clean worktree on conflict."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from harness.stack_refresh import RefreshResult, guarded_refresh, refresh_branch, refresh_reason, refresh_record

BASE_TEXT = "".join(f"line {n}\n" for n in range(1, 21))


def run(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def commit_on_main(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", f"main {name}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    run(path, "init", "-q", "-b", "main")
    run(path, "config", "user.name", "t")
    run(path, "config", "user.email", "t@example.com")
    (path / "shared.txt").write_text(BASE_TEXT)
    run(path, "add", "-A")
    run(path, "commit", "-q", "-m", "base")
    return path


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    run(repo, "worktree", "add", "-q", "-b", "level", str(path), "main")
    run(path, "config", "user.name", "t")
    run(path, "config", "user.email", "t@example.com")
    return path


def test_branch_cut_before_a_main_commit_gains_it(repo: Path, worktree: Path) -> None:
    commit_on_main(repo, "landed.txt", "landed\n")
    result = refresh_branch(worktree, "level", "main")
    assert result.ok and result.refreshed
    assert result.paths == () and result.reason == ""
    assert result.head_before != result.head_after
    assert result.head_after == run(repo, "rev-parse", "level")
    assert (worktree / "landed.txt").exists()


def test_up_to_date_branch_is_left_alone(repo: Path, worktree: Path) -> None:
    before = run(repo, "rev-parse", "level")
    result = refresh_branch(worktree, "level", "main")
    assert result.ok and not result.refreshed
    assert result.head_before == result.head_after == before
    assert run(repo, "rev-parse", "level") == before


def test_conflict_names_the_file_and_leaves_a_clean_worktree(repo: Path, worktree: Path) -> None:
    (worktree / "shared.txt").write_text(BASE_TEXT.replace("line 5\n", "level five\n"))
    run(worktree, "commit", "-q", "-am", "level edit")
    commit_on_main(repo, "shared.txt", BASE_TEXT.replace("line 5\n", "main five\n"))
    before = run(repo, "rev-parse", "level")
    result = refresh_branch(worktree, "level", "main")
    assert not result.ok and not result.refreshed
    assert result.paths == ("shared.txt",)
    assert "shared.txt" in result.reason
    assert run(worktree, "status", "--porcelain") == ""
    assert run(repo, "rev-parse", "level") == before


def test_other_checked_out_branch_is_refused(repo: Path, worktree: Path) -> None:
    result = refresh_branch(worktree, "elsewhere", "main")
    assert not result.ok and not result.refreshed
    assert "elsewhere" in result.reason


def test_result_is_frozen() -> None:
    result = RefreshResult(True, False, "a", "a", (), "")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]


def test_reason_names_every_path_sorted() -> None:
    assert refresh_reason("task/x", "main", ["b.py", "a.py"]) == (
        "merging main into task/x conflicted in: a.py, b.py; the level was not built on the stale tree"
    )


def test_record_is_one_line() -> None:
    line = refresh_record("task/x", "main", "abc", "def")
    assert line == "refreshed task/x onto main: head abc became def"
    assert "\n" not in line


# ── a refresh keeps every task merge the store recorded ──────────────────────


def merge_task(repo: Path, worktree: Path, task: str, name: str) -> tuple[str, str]:
    """A task branch off `level` with one commit, merged into `level` with a merge commit; (task commit, merge commit)."""
    run(repo, "branch", task, "level")
    run(repo, "worktree", "add", "-q", str(repo.parent / task), task)
    (repo.parent / task / name).write_text(f"{task}\n")
    run(repo.parent / task, "add", "-A")
    run(repo.parent / task, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", f"task {task}")
    built = run(repo, "rev-parse", task)
    run(worktree, "merge", "--no-ff", "-q", "-m", f"merge {task}", task)
    return built, run(worktree, "rev-parse", "HEAD")


def is_ancestor(repo: Path, commit: str, ref: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", commit, ref], cwd=repo).returncode == 0


def test_refresh_onto_advanced_main_keeps_the_task_merge(repo: Path, worktree: Path) -> None:
    built, merge = merge_task(repo, worktree, "task-a", "a.txt")
    commit_on_main(repo, "landed.txt", "landed\n")
    diff_before = run(repo, "diff", "main...level", "--stat")

    result = guarded_refresh(worktree, "level", "main", [merge])

    assert result.ok and result.refreshed
    assert is_ancestor(repo, built, "level") and is_ancestor(repo, merge, "level")
    assert is_ancestor(repo, "main", "level")
    second_parents = {
        line.split()[2]
        for line in run(repo, "rev-list", "--parents", "--merges", "level").splitlines()
        if line.count(" ") == 2
    }
    assert built in second_parents
    assert run(repo, "diff", "main...level", "--stat") == diff_before


def linear_rebase(worktree: Path, branch: str, onto: str) -> RefreshResult:
    """The old refresh: a plain rebase, which linearises the branch and drops its merge commits."""
    before = run(worktree, "rev-parse", "HEAD")
    run(worktree, "-c", "user.name=t", "-c", "user.email=t@example.com", "rebase", "-q", onto)
    return RefreshResult(True, True, before, run(worktree, "rev-parse", "HEAD"), (), "")


def test_refresh_that_would_drop_a_recorded_merge_refuses(repo: Path, worktree: Path) -> None:
    _, merge = merge_task(repo, worktree, "task-a", "a.txt")
    commit_on_main(repo, "landed.txt", "landed\n")
    before = run(repo, "rev-parse", "level")

    result = guarded_refresh(worktree, "level", "main", [merge], refresh=linear_rebase)

    assert not result.ok and not result.refreshed
    assert merge[:12] in result.reason and "would drop" in result.reason
    assert run(repo, "rev-parse", "level") == before
    assert run(worktree, "status", "--porcelain") == ""


def test_a_recorded_merge_already_off_the_branch_does_not_block(repo: Path, worktree: Path) -> None:
    """Only what the branch holds before the refresh is guarded; a commit it never held is not this refresh's loss."""
    stray = run(repo, "commit-tree", "-m", "never on level", "main^{tree}")
    commit_on_main(repo, "landed.txt", "landed\n")
    result = guarded_refresh(worktree, "level", "main", [stray])
    assert result.ok and result.refreshed
