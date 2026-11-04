from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_tools.checkout_sync import decide, git_runner, report, sync_checkout


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin with one commit on main, and a clone of it at that commit."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    git(seed, "checkout", "-b", "main")
    commit(seed, "a")
    git(seed, "push", "origin", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    return seed, clone


def advance_origin(seed: Path) -> str:
    tip = commit(seed, "b")
    git(seed, "push", "origin", "main")
    return tip


def test_decide_fast_forward() -> None:
    assert decide(True, "main", "main", True, False) == "fast_forward"


def test_decide_already_current() -> None:
    assert decide(True, "main", "main", True, True) == "already_current"


def test_decide_dirty() -> None:
    assert decide(False, "main", "main", True, False) == "dirty"


def test_decide_wrong_branch() -> None:
    assert decide(True, "topic", "main", True, False) == "wrong_branch"


def test_decide_detached_is_wrong_branch() -> None:
    assert decide(True, None, "main", True, False) == "wrong_branch"


def test_decide_diverged() -> None:
    assert decide(True, "main", "main", False, False) == "diverged"


def test_report_lines() -> None:
    assert report("dirty", "main", "main") == (
        "checkout not fast-forwarded: working tree has uncommitted changes (branch main)"
    )
    assert report("wrong_branch", "topic", "main") == "checkout not fast-forwarded: on topic, not main"
    assert report("diverged", "main", "main") == (
        "checkout not fast-forwarded: main has local commits that origin/main lacks"
    )
    assert report("fast_forward", "main", "main") is None
    assert report("already_current", "main", "main") is None


def test_dirty_checkout_is_reported_and_head_unchanged(repos: tuple[Path, Path]) -> None:
    seed, clone = repos
    advance_origin(seed)
    git(clone, "fetch", "origin")
    (clone / "a").write_text("edited")
    before = git(clone, "rev-parse", "HEAD")
    verdict, line = sync_checkout(clone, "main", git_runner)
    assert verdict == "dirty"
    assert line is not None and "uncommitted" in line
    assert git(clone, "rev-parse", "HEAD") == before
    assert (clone / "a").read_text() == "edited"


def test_clean_checkout_behind_origin_is_fast_forwarded(repos: tuple[Path, Path]) -> None:
    seed, clone = repos
    tip = advance_origin(seed)
    git(clone, "fetch", "origin")
    verdict, line = sync_checkout(clone, "main", git_runner)
    assert (verdict, line) == ("fast_forward", None)
    assert git(clone, "rev-parse", "HEAD") == tip
    assert len(git(clone, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 2  # one parent: no merge commit


def test_clean_checkout_at_tip_is_already_current(repos: tuple[Path, Path]) -> None:
    _, clone = repos
    before = git(clone, "rev-parse", "HEAD")
    assert sync_checkout(clone, "main", git_runner) == ("already_current", None)
    assert git(clone, "rev-parse", "HEAD") == before


def test_diverged_checkout_is_reported_and_not_moved(repos: tuple[Path, Path]) -> None:
    seed, clone = repos
    advance_origin(seed)
    local = commit(clone, "local")
    git(clone, "fetch", "origin")
    verdict, line = sync_checkout(clone, "main", git_runner)
    assert verdict == "diverged"
    assert line is not None and "origin/main" in line
    assert git(clone, "rev-parse", "HEAD") == local


def test_other_branch_is_reported_and_not_moved(repos: tuple[Path, Path]) -> None:
    seed, clone = repos
    advance_origin(seed)
    git(clone, "fetch", "origin")
    git(clone, "checkout", "-b", "topic")
    before = git(clone, "rev-parse", "HEAD")
    verdict, line = sync_checkout(clone, "main", git_runner)
    assert verdict == "wrong_branch"
    assert line == "checkout not fast-forwarded: on topic, not main"
    assert git(clone, "rev-parse", "HEAD") == before


@pytest.mark.parametrize("scenario", ["dirty", "wrong_branch", "diverged", "already_current"])
def test_no_merge_is_issued_unless_fast_forward(repos: tuple[Path, Path], scenario: str) -> None:
    seed, clone = repos
    if scenario != "already_current":
        advance_origin(seed)
        git(clone, "fetch", "origin")
    if scenario == "dirty":
        (clone / "a").write_text("edited")
    elif scenario == "wrong_branch":
        git(clone, "checkout", "-b", "topic")
    elif scenario == "diverged":
        commit(clone, "local")
    calls: list[tuple[str, ...]] = []

    def recording(repo: Path, args: Sequence[str]) -> tuple[int, str]:
        calls.append(tuple(args))
        return git_runner(repo, args)

    verdict, _ = sync_checkout(clone, "main", recording)
    assert verdict == scenario
    assert not any(args[0] == "merge" for args in calls)


def test_refused_fast_forward_is_reported() -> None:
    answers = {
        "status": (0, ""),
        "symbolic-ref": (0, "main\n"),
        "rev-parse": (0, "abc\n"),
        "merge-base": (0, ""),
        "merge": (1, "fatal: Not possible to fast-forward, aborting.\n"),
    }

    def fake(repo: Path, args: Sequence[str]) -> tuple[int, str]:
        if args[0] == "rev-parse" and args[1] == "origin/main":
            return 0, "def\n"
        return answers[args[0]]

    verdict, line = sync_checkout(Path("."), "main", fake)
    assert verdict == "fast_forward"
    assert line == "checkout fast-forward to origin/main failed: fatal: Not possible to fast-forward, aborting."
