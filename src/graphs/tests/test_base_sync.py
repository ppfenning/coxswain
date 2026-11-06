"""The base-sync edge tests a commit by ancestry and by patch-id, merges absent ones in, and never raises on fetch."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.base_sync import fetch_origin, holds, merge_in

BASE_TEXT = "".join(f"line {n}\n" for n in range(1, 21))


def run(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def commit_file(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text)
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", message)
    return run(repo, "rev-parse", "HEAD")


def branch_commit(repo: Path, name: str, path: str, text: str) -> str:
    run(repo, "checkout", "-q", "-b", name, "main")
    sha = commit_file(repo, path, text, name)
    run(repo, "checkout", "-q", "main")
    return sha


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


def make_worktree(repo: Path, tmp_path: Path, branch: str = "work") -> Path:
    path = tmp_path / "wt"
    run(repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
    return path


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    return make_worktree(repo, tmp_path)


def test_holds_true_for_an_ancestor(repo: Path, worktree: Path) -> None:
    assert holds(worktree, run(repo, "rev-parse", "main"))


def test_holds_false_for_a_commit_on_another_branch(repo: Path, worktree: Path) -> None:
    other = branch_commit(repo, "other", "other.txt", "x\n")
    assert not holds(worktree, other)


def test_holds_true_for_a_squash_merged_commit_with_the_same_change(repo: Path, worktree: Path) -> None:
    feature = branch_commit(repo, "feature", "feature.txt", "x\n")
    run(repo, "merge", "--squash", "feature")
    run(repo, "commit", "-q", "-m", "squashed")
    run(worktree, "merge", "-q", "--ff-only", "main")
    assert run(worktree, "rev-parse", "HEAD") != feature
    assert holds(worktree, feature)


def test_holds_false_for_an_unknown_sha(worktree: Path) -> None:
    assert not holds(worktree, "0123456789abcdef0123456789abcdef01234567")


def test_merge_in_brings_an_absent_commit_and_keeps_own_commits(repo: Path, worktree: Path) -> None:
    other = branch_commit(repo, "other", "other.txt", "x\n")
    own = commit_file(worktree, "own.txt", "mine\n", "own")
    assert merge_in(worktree, [other]) == (True, "")
    assert holds(worktree, other)
    assert run(worktree, "merge-base", "--is-ancestor", own, "HEAD") == ""
    assert len(run(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3


def test_merge_in_skips_a_held_commit(repo: Path, worktree: Path) -> None:
    held = run(repo, "rev-parse", "main")
    commit_file(worktree, "own.txt", "mine\n", "own")
    before = run(worktree, "rev-parse", "HEAD")
    assert merge_in(worktree, [held]) == (True, "")
    assert run(worktree, "rev-parse", "HEAD") == before


def test_merge_in_conflict_names_commit_and_files_and_leaves_worktree_clean(repo: Path, worktree: Path) -> None:
    theirs = branch_commit(repo, "theirs", "shared.txt", BASE_TEXT.replace("line 5\n", "theirs\n"))
    commit_file(worktree, "shared.txt", BASE_TEXT.replace("line 5\n", "mine\n"), "own")
    before = run(worktree, "rev-parse", "HEAD")
    ok, cause = merge_in(worktree, [theirs])
    assert not ok
    assert theirs in cause and "shared.txt" in cause
    assert run(worktree, "status", "--porcelain") == ""
    assert run(worktree, "rev-parse", "HEAD") == before
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=worktree).returncode != 0


def test_fetch_origin_succeeds_with_no_remote(repo: Path) -> None:
    ok, detail = fetch_origin(repo)
    assert ok and detail


def test_fetch_origin_reports_an_unreachable_remote_without_raising(repo: Path, tmp_path: Path) -> None:
    run(repo, "remote", "add", "origin", str(tmp_path / "missing"))
    ok, detail = fetch_origin(repo)
    assert not ok and detail
