"""Phase branches are pushed to origin and recovered from it on a relaunch."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.phase_remote import (
    FetchOutcome,
    Kind,
    fetch_branch,
    fetched_ref,
    materialise_local,
    missing_branch_cause,
    push_branch,
    refresh_verdict,
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


def test_a_local_branch_is_kept_as_it_is_when_origin_cannot_be_asked(tmp_path: Path) -> None:
    repo, _ = with_origin(tmp_path)
    tip = git("rev-parse", PHASE, cwd=repo)
    break_origin(repo, tmp_path)
    outcome = fetch_branch(repo, PHASE)
    assert outcome.kind == Kind.PRESENT_LOCALLY and outcome.detail.startswith("not refreshed from origin")
    assert git("rev-parse", PHASE, cwd=repo) == tip


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


def test_missing_branch_cause_names_the_branch() -> None:
    assert (
        missing_branch_cause(PHASE, FetchOutcome(Kind.MISSING_EVERYWHERE))
        == f"phase branch {PHASE} exists neither locally nor on origin"
    )
    assert PHASE in missing_branch_cause(PHASE, FetchOutcome(Kind.FETCH_FAILED, "fatal: unreachable\nmore"))


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git("add", name, cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def second_machine(tmp_path: Path, origin: Path) -> Path:
    """A clone of origin with the phase branch local, as a lane host holds it after an earlier build."""
    git("clone", "-q", str(origin), "host", cwd=tmp_path)
    host = tmp_path / "host"
    git("config", "user.name", "fixture", cwd=host)
    git("config", "user.email", "fixture@invalid", cwd=host)
    git("branch", PHASE, f"origin/{PHASE}", cwd=host)
    return host


def advance_origin(repo: Path, name: str) -> str:
    """A commit on the phase branch pushed to origin from `repo`, like a repaired merge from another machine."""
    git("checkout", "-q", PHASE, cwd=repo)
    tip = commit(repo, name)
    git("checkout", "-q", "main", cwd=repo)
    assert push_branch(repo, PHASE)[0] is True
    return tip


@pytest.mark.parametrize(("local_in_origin", "origin_in_local", "kind"), [
    (True, False, Kind.FAST_FORWARDED),
    (False, True, Kind.PRESENT_LOCALLY),
    (True, True, Kind.PRESENT_LOCALLY),
    (False, False, Kind.DIVERGED),
])
def test_refresh_verdict_only_ever_moves_a_local_phase_branch_forward(local_in_origin, origin_in_local, kind) -> None:
    assert refresh_verdict(local_in_origin, origin_in_local) == kind


def test_a_phase_branch_missing_locally_is_created_from_origin(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    git("clone", "-q", str(origin), "host", cwd=tmp_path)
    host = tmp_path / "host"
    assert fetch_branch(host, PHASE) == FetchOutcome(Kind.FETCHED)
    assert materialise_local(host, PHASE)[0] is True
    assert git("rev-parse", f"refs/heads/{PHASE}", cwd=host) == git("rev-parse", PHASE, cwd=repo)


def test_a_stale_local_phase_branch_is_fast_forwarded_to_origin(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    host = second_machine(tmp_path, origin)
    repaired = advance_origin(repo, "repaired.txt")
    outcome = fetch_branch(host, PHASE)
    assert outcome.kind == Kind.FAST_FORWARDED
    assert git("rev-parse", f"refs/heads/{PHASE}", cwd=host) == repaired
    assert not has_ref(host, fetched_ref(PHASE))


def test_a_checked_out_stale_phase_branch_fast_forwards_with_its_files(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    host = second_machine(tmp_path, origin)
    git("worktree", "add", "-q", str(tmp_path / "phase-wt"), PHASE, cwd=host)
    repaired = advance_origin(repo, "repaired.txt")
    assert fetch_branch(host, PHASE).kind == Kind.FAST_FORWARDED
    assert git("rev-parse", "HEAD", cwd=tmp_path / "phase-wt") == repaired
    assert (tmp_path / "phase-wt" / "repaired.txt").is_file()
    assert git("status", "--porcelain", cwd=tmp_path / "phase-wt") == ""


def test_a_local_phase_branch_ahead_of_origin_is_kept(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    host = second_machine(tmp_path, origin)
    git("checkout", "-q", PHASE, cwd=host)
    merged = commit(host, "merged.txt")
    git("checkout", "-q", "--detach", cwd=host)
    assert fetch_branch(host, PHASE) == FetchOutcome(Kind.PRESENT_LOCALLY)
    assert git("rev-parse", f"refs/heads/{PHASE}", cwd=host) == merged
    assert not has_ref(host, fetched_ref(PHASE))


def test_a_diverged_phase_branch_keeps_local_and_saves_origins_tip(tmp_path: Path) -> None:
    repo, origin = with_origin(tmp_path)
    push_branch(repo, PHASE)
    host = second_machine(tmp_path, origin)
    git("checkout", "-q", PHASE, cwd=host)
    merged = commit(host, "merged.txt")
    git("checkout", "-q", "--detach", cwd=host)
    repaired = advance_origin(repo, "repaired.txt")
    outcome = fetch_branch(host, PHASE)
    assert outcome.kind == Kind.DIVERGED
    assert git("rev-parse", f"refs/heads/{PHASE}", cwd=host) == merged
    assert git("rev-parse", fetched_ref(PHASE), cwd=host) == repaired
    assert fetched_ref(PHASE) == f"refs/coxswain/fetched/origin/{PHASE}"
    assert PHASE in outcome.detail and fetched_ref(PHASE) in outcome.detail
