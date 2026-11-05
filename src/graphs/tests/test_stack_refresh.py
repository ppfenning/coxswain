"""Stack refresh merges the default ref into a level branch and leaves a clean worktree on conflict."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from harness.stack_refresh import RefreshResult, refresh_branch, refresh_reason, refresh_record

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
