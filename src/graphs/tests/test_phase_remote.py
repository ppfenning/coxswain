"""Phase branches are pushed to origin and recovered from it on a relaunch."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.phase_remote import (
    FetchOutcome,
    Kind,
    fetch_branch,
    fetch_branch_tip,
    materialise_local,
    missing_branch_cause,
    push_branch,
)

PHASE = "epic/demo-initiative/p1-foundations"
ABSENT = "epic/demo-initiative/p9-absent"


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def has_ref(repo: Path, ref: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo).returncode == 0


def make_repo(path: Path) -> Path:
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.name", "fixture", cwd=path)
    git("config", "user.email", "fixture@invalid", cwd=path)
    (path / "a.txt").write_text("a\n")
    git("add", "a.txt", cwd=path)
    git("commit", "-q", "-m", "first", cwd=path)
    return path


def with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with a phase branch, plus a bare origin it is wired to."""
    repo = make_repo(tmp_path / "repo")
    git("branch", PHASE, cwd=repo)
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("remote", "add", "origin", str(origin), cwd=repo)
    return repo, origin


def break_origin(repo: Path, tmp_path: Path) -> None:
    git("remote", "set-url", "origin", str(tmp_path / "nowhere.git"), cwd=repo)


def test_push_lands_the_branch_on_origin(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    assert push_branch(repo, PHASE)[0] is True
    assert git("rev-parse", f"refs/heads/{PHASE}", cwd=origin) == git("rev-parse", PHASE, cwd=repo)


def test_a_second_machine_fetches_and_materialises_the_branch(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    other = tmp_path / "other"
    other.mkdir()
    git("init", "-q", "-b", "main", cwd=other)
    git("remote", "add", "origin", str(origin), cwd=other)
    assert not has_ref(other, f"refs/remotes/origin/{PHASE}")
    assert fetch_branch(other, PHASE) == FetchOutcome(Kind.FETCHED)
    assert git("rev-parse", f"refs/remotes/origin/{PHASE}", cwd=other) == git("rev-parse", PHASE, cwd=repo)
    assert materialise_local(other, PHASE)[0] is True
    assert git("rev-parse", f"refs/heads/{PHASE}", cwd=other) == git("rev-parse", PHASE, cwd=repo)


def test_a_local_branch_is_present_without_touching_origin(tmp_path: Path) -> None:
    repo, _ = with_origin(tmp_path)
    break_origin(repo, tmp_path)
    assert fetch_branch(repo, PHASE) == FetchOutcome(Kind.PRESENT_LOCALLY)


def test_a_branch_absent_from_both_is_missing_everywhere(tmp_path: Path) -> None:
    repo, _ = with_origin(tmp_path)
    assert fetch_branch(repo, ABSENT).kind == Kind.MISSING_EVERYWHERE


def test_an_unreachable_origin_is_fetch_failed_not_missing(tmp_path: Path) -> None:
    repo, _ = with_origin(tmp_path)
    break_origin(repo, tmp_path)
    outcome = fetch_branch(repo, ABSENT)
    assert outcome.kind == Kind.FETCH_FAILED
    assert outcome.detail != ""


def test_a_repo_with_no_origin_pushes_as_a_no_op(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    git("branch", PHASE, cwd=repo)
    assert push_branch(repo, PHASE) == (True, "no origin")


def test_a_git_failure_is_not_read_as_no_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    pushed, detail = push_branch(plain, PHASE)
    assert pushed is False
    assert detail != "no origin"
    assert fetch_branch(plain, PHASE).kind == Kind.FETCH_FAILED


def test_a_rejected_non_fast_forward_push_is_reported(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    git("clone", "-q", str(origin), "other", cwd=tmp_path)
    other = tmp_path / "other"
    git("config", "user.name", "fixture", cwd=other)
    git("config", "user.email", "fixture@invalid", cwd=other)
    git("checkout", "-q", PHASE, cwd=other)
    (other / "b.txt").write_text("b\n")
    git("add", "b.txt", cwd=other)
    git("commit", "-q", "-m", "ahead", cwd=other)
    git("push", "-q", "origin", PHASE, cwd=other)
    git("checkout", "-q", PHASE, cwd=repo)
    (repo / "c.txt").write_text("c\n")
    git("add", "c.txt", cwd=repo)
    git("commit", "-q", "-m", "diverged", cwd=repo)
    assert push_branch(repo, PHASE)[0] is False


def failing_fetch(monkeypatch: pytest.MonkeyPatch, outcome: subprocess.TimeoutExpired | int) -> list[list[str]]:
    """Make every `git fetch` fail by timeout or exit code; the list records the git commands run."""
    real, calls = subprocess.run, []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if "fetch" not in cmd:
            return real(cmd, **kwargs)
        if isinstance(outcome, subprocess.TimeoutExpired):
            raise outcome
        return subprocess.CompletedProcess(cmd, outcome, "", "fatal: transient failure")

    monkeypatch.setattr(subprocess, "run", fake)
    return calls


def test_a_fetch_timeout_is_fetch_failed_and_skips_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _ = with_origin(tmp_path)
    calls = failing_fetch(monkeypatch, subprocess.TimeoutExpired("git", 120))
    outcome = fetch_branch(repo, ABSENT)
    assert outcome.kind == Kind.FETCH_FAILED
    assert "timed out" in outcome.detail
    assert not any("ls-remote" in cmd for cmd in calls)


def test_a_failed_fetch_of_a_branch_origin_holds_is_fetch_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    other = tmp_path / "other"
    other.mkdir()
    git("init", "-q", "-b", "main", cwd=other)
    git("remote", "add", "origin", str(origin), cwd=other)
    failing_fetch(monkeypatch, 1)
    assert fetch_branch(other, PHASE) == FetchOutcome(Kind.FETCH_FAILED, "fatal: transient failure")


def test_a_stale_local_branch_is_refreshed_from_origin(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    git("clone", "-q", str(origin), "other", cwd=tmp_path)
    other = tmp_path / "other"
    git("checkout", "-q", PHASE, cwd=other)
    stale = git("rev-parse", f"refs/heads/{PHASE}", cwd=other)
    git("checkout", "-q", PHASE, cwd=repo)
    (repo / "b.txt").write_text("b\n")
    git("add", "b.txt", cwd=repo)
    git("commit", "-q", "-m", "ahead", cwd=repo)
    push_branch(repo, PHASE)
    assert fetch_branch_tip(other, PHASE) == FetchOutcome(Kind.FETCHED)
    new_tip = git("rev-parse", f"refs/heads/{PHASE}", cwd=origin)
    assert git("rev-parse", f"refs/remotes/origin/{PHASE}", cwd=other) == new_tip
    assert new_tip != stale
    assert git("rev-parse", f"refs/heads/{PHASE}", cwd=other) == stale


def test_a_repo_with_no_origin_has_no_tip_to_fetch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    assert fetch_branch_tip(repo, PHASE).kind == Kind.MISSING_EVERYWHERE


def test_an_origin_without_the_branch_is_missing_for_a_tip_fetch(tmp_path: Path) -> None:
    repo, _ = with_origin(tmp_path)
    assert fetch_branch_tip(repo, ABSENT).kind == Kind.MISSING_EVERYWHERE


def test_an_unreadable_origin_is_fetch_failed_for_a_tip_fetch(tmp_path: Path) -> None:
    repo, _ = with_origin(tmp_path)
    break_origin(repo, tmp_path)
    outcome = fetch_branch_tip(repo, PHASE)
    assert outcome.kind == Kind.FETCH_FAILED
    assert outcome.detail != ""


def test_missing_branch_cause_names_the_branch() -> None:
    assert (
        missing_branch_cause(PHASE, FetchOutcome(Kind.MISSING_EVERYWHERE))
        == f"phase branch {PHASE} exists neither locally nor on origin"
    )
    assert PHASE in missing_branch_cause(PHASE, FetchOutcome(Kind.FETCH_FAILED, "fatal: unreachable\nmore"))
