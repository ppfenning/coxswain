import subprocess
from pathlib import Path

import pytest

from agent_tools.chair_steward_level import LevelState, level_merged, level_state


def test_ancestor_is_merged():
    assert level_merged(True, "bbb", "aaa") is LevelState.MERGED


def test_ancestor_wins_over_a_failed_trial():
    assert level_merged(True, None, "aaa") is LevelState.MERGED


def test_failed_trial_is_conflict():
    assert level_merged(False, None, "aaa") is LevelState.CONFLICT


def test_trial_equal_to_ticket_tree_is_merged():
    assert level_merged(False, "aaa", "aaa") is LevelState.MERGED


def test_trial_differing_from_ticket_tree_is_not_merged():
    assert level_merged(False, "bbb", "aaa") is LevelState.NOT_MERGED


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout


def run_git(args: list[str]) -> tuple[int, str]:
    done = subprocess.run(["git", *args], capture_output=True, text=True)
    return done.returncode, done.stdout


def commit_file(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"{name}: {text.strip()}")


def snapshot(repo: Path) -> tuple[str, str, str, bytes]:
    return (
        git(repo, "for-each-ref"),
        git(repo, "symbolic-ref", "HEAD"),
        git(repo, "--no-optional-locks", "status", "--porcelain"),
        (repo / ".git" / "index").read_bytes(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "commit.gpgsign", "false")
    commit_file(tmp_path, "shared.txt", "one\ntwo\nthree\n")
    git(tmp_path, "branch", "ticket")
    git(tmp_path, "branch", "level")
    return tmp_path


def level_commits(repo: Path) -> None:
    git(repo, "checkout", "level")
    commit_file(repo, "a.txt", "a\n")
    commit_file(repo, "b.txt", "b\n")
    git(repo, "checkout", "ticket")


def classify(repo: Path) -> LevelState:
    before = snapshot(repo)
    state = level_state(str(repo), "ticket", "level", run_git)
    assert snapshot(repo) == before
    return state


def test_regular_merge_is_merged_by_ancestry(repo: Path):
    level_commits(repo)
    git(repo, "merge", "--no-ff", "-m", "land level", "level")
    assert run_git(["-C", str(repo), "merge-base", "--is-ancestor", "level", "ticket"])[0] == 0
    assert classify(repo) is LevelState.MERGED


def test_squash_merge_is_merged_by_content(repo: Path):
    level_commits(repo)
    git(repo, "merge", "--squash", "level")
    git(repo, "commit", "-m", "land level as one commit")
    assert run_git(["-C", str(repo), "merge-base", "--is-ancestor", "level", "ticket"])[0] == 1
    assert classify(repo) is LevelState.MERGED


def test_unmerged_level_is_not_merged(repo: Path):
    level_commits(repo)
    commit_file(repo, "ticket-only.txt", "t\n")
    assert classify(repo) is LevelState.NOT_MERGED


def test_conflicting_level_is_conflict(repo: Path):
    git(repo, "checkout", "level")
    commit_file(repo, "shared.txt", "one\nlevel\nthree\n")
    git(repo, "checkout", "ticket")
    commit_file(repo, "shared.txt", "one\nticket\nthree\n")
    assert classify(repo) is LevelState.CONFLICT


def test_unknown_branch_raises(repo: Path):
    with pytest.raises(RuntimeError):
        level_state(str(repo), "ticket", "nope", run_git)
