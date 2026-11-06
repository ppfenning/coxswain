"""The carry edge tests ancestry, finds a change on the base by patch-id, fetches absent commits, and cherry-picks without raising."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.carry_git import cherry_pick, fetch_commit, landed_on_base, on_branch

BASE_TEXT = "".join(f"line {n}\n" for n in range(1, 21))
UNKNOWN = "0123456789abcdef0123456789abcdef01234567"


def run(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def identify(path: Path) -> None:
    run(path, "config", "user.name", "t")
    run(path, "config", "user.email", "t@example.com")


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
def origin(tmp_path: Path) -> Path:
    path = tmp_path / "origin.git"
    path.mkdir()
    run(path, "init", "-q", "--bare", "-b", "main")
    return path


@pytest.fixture
def repo(tmp_path: Path, origin: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    run(path, "init", "-q", "-b", "main")
    identify(path)
    (path / "shared.txt").write_text(BASE_TEXT)
    run(path, "add", "-A")
    run(path, "commit", "-q", "-m", "base")
    run(path, "remote", "add", "origin", str(origin))
    run(path, "push", "-q", "origin", "main")
    return path


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    run(repo, "worktree", "add", "-q", "-b", "work", str(path), "main")
    return path


def clone_of(origin: Path, tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    run(tmp_path, "clone", "-q", str(origin), str(path))
    identify(path)
    return path


def test_on_branch_true_for_an_ancestor(repo: Path) -> None:
    assert on_branch(repo, run(repo, "rev-parse", "main"), "main")


def test_on_branch_false_for_another_branch_and_for_an_unknown_commit(repo: Path) -> None:
    other = branch_commit(repo, "other", "other.txt", "x\n")
    assert not on_branch(repo, other, "main")
    assert not on_branch(repo, UNKNOWN, "main")


def test_fetch_commit_gets_a_commit_that_is_only_on_origin(repo: Path, origin: Path, tmp_path: Path) -> None:
    builder = clone_of(origin, tmp_path, "builder")
    run(builder, "checkout", "-q", "-b", "run-1/phase-a")
    sha = commit_file(builder, "built.txt", "x\n", "built")
    run(builder, "push", "-q", "origin", "run-1/phase-a")
    assert not on_branch(repo, sha, "main")
    assert fetch_commit(repo, sha, "run-1", [])
    assert run(repo, "cat-file", "-t", sha) == "commit"


def test_fetch_commit_gets_a_commit_from_a_host_source(repo: Path, tmp_path: Path) -> None:
    host = tmp_path / "host"
    run(tmp_path, "clone", "-q", str(repo), str(host))
    identify(host)
    sha = commit_file(host, "host.txt", "x\n", "host only")
    assert fetch_commit(repo, sha, "run-1", [str(host)])
    assert run(repo, "cat-file", "-t", sha) == "commit"


def test_fetch_commit_false_for_an_unknown_commit(repo: Path, tmp_path: Path) -> None:
    assert not fetch_commit(repo, UNKNOWN, "run-1", [str(tmp_path / "missing")])


def test_cherry_pick_lands_a_clean_commit_and_moves_head(repo: Path, worktree: Path) -> None:
    other = branch_commit(repo, "other", "other.txt", "x\n")
    before = run(worktree, "rev-parse", "HEAD")
    ok, head = cherry_pick(worktree, other)
    assert ok
    assert head == run(worktree, "rev-parse", "HEAD") != before
    assert (worktree / "other.txt").read_text() == "x\n"
    assert run(worktree, "status", "--porcelain") == ""


def test_cherry_pick_conflict_leaves_the_worktree_clean_and_names_the_file(repo: Path, worktree: Path) -> None:
    theirs = branch_commit(repo, "other", "shared.txt", BASE_TEXT.replace("line 1\n", "theirs\n"))
    identify(worktree)
    commit_file(worktree, "shared.txt", BASE_TEXT.replace("line 1\n", "ours\n"), "ours")
    before = run(worktree, "rev-parse", "HEAD")
    ok, cause = cherry_pick(worktree, theirs)
    assert not ok
    assert "shared.txt" in cause
    assert run(worktree, "status", "--porcelain") == ""
    assert run(worktree, "rev-parse", "HEAD") == before


def test_landed_on_base_returns_the_pull_request_of_a_squash_with_a_different_id(repo: Path) -> None:
    feature = branch_commit(repo, "feature", "feature.txt", "x\n")
    run(repo, "merge", "--squash", "feature")
    run(repo, "commit", "-q", "-m", "feature (#42)")
    assert run(repo, "rev-parse", "main") != feature
    assert landed_on_base(repo, feature, "main") == 42


def test_landed_on_base_none_when_the_change_is_absent(repo: Path) -> None:
    feature = branch_commit(repo, "feature", "feature.txt", "x\n")
    assert landed_on_base(repo, feature, "main") is None
    assert landed_on_base(repo, UNKNOWN, "main") is None
