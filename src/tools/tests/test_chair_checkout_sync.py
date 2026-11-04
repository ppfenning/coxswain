from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_tools import cli


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def stale(tmp_path: Path) -> tuple[Path, str]:
    """A clone one commit behind origin/main, and the commit origin has."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    git(seed, "checkout", "-b", "main")
    (seed / "a").write_text("a")
    git(seed, "add", "a")
    git(seed, "commit", "-m", "a")
    git(seed, "push", "origin", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    (seed / "b").write_text("b")
    git(seed, "add", "b")
    git(seed, "commit", "-m", "b")
    git(seed, "push", "origin", "main")
    return clone, git(seed, "rev-parse", "HEAD")


def ticks(repo: Path, reports: cli._CheckoutReports, lines: list[str], count: int) -> None:
    for _ in range(count):
        cli._sync_chair_checkouts([str(repo)], reports, lines.append)


def test_dirty_checkout_reports_once_across_three_ticks_and_is_not_touched(stale: tuple[Path, str]) -> None:
    clone, _ = stale
    before = git(clone, "rev-parse", "HEAD")
    (clone / "a").write_text("edited")
    lines: list[str] = []
    ticks(clone, cli._CheckoutReports(), lines, 3)
    assert len(lines) == 1
    assert "uncommitted changes" in lines[0]
    assert git(clone, "rev-parse", "HEAD") == before


def test_clean_stale_checkout_is_fast_forwarded_on_the_first_tick_then_silent(stale: tuple[Path, str]) -> None:
    clone, tip = stale
    lines: list[str] = []
    reports = cli._CheckoutReports()
    ticks(clone, reports, lines, 1)
    assert git(clone, "rev-parse", "HEAD") == tip
    ticks(clone, reports, lines, 2)
    assert lines == []


def test_a_sync_failure_does_not_raise_and_is_reported_once(tmp_path: Path) -> None:
    def broken(repo: Path, args: object) -> tuple[int, str]:
        raise RuntimeError("git exploded")

    lines: list[str] = []
    reports = cli._CheckoutReports()
    for _ in range(2):
        cli._sync_chair_checkouts([str(tmp_path)], reports, lines.append, run=broken)
    assert len(lines) == 1
    assert "git exploded" in lines[0]


def test_one_sync_per_repo_per_tick(stale: tuple[Path, str]) -> None:
    clone, _ = stale
    calls: list[Path] = []

    def counting(repo: Path, args: list[str]) -> tuple[int, str]:
        if args[0] == "symbolic-ref":
            calls.append(repo)
        return 1, "no"

    cli._sync_chair_checkouts([str(clone), str(clone)], cli._CheckoutReports(), [].append, run=counting)
    assert calls == [clone]
