"""Edge helpers that keep a phase branch on origin, so a relaunch on another machine can find it."""

from __future__ import annotations

import os
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

# `git ls-remote --exit-code` exits 2 when origin answered and holds no matching ref.
_LS_REMOTE_NO_MATCH = 2
_TIMED_OUT = -1


class Kind(StrEnum):
    PRESENT_LOCALLY = "present_locally"
    FETCHED = "fetched"
    MISSING_EVERYWHERE = "missing_everywhere"
    FETCH_FAILED = "fetch_failed"


class FetchOutcome(NamedTuple):
    kind: Kind
    detail: str = ""


def _run(*args: str, cwd: Path) -> tuple[int, str]:
    """Run one git command that can never wait on a prompt; the code is -1 on timeout."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        return _TIMED_OUT, f"git {' '.join(args)} timed out after 120s"
    return proc.returncode, (proc.stderr or proc.stdout).strip()


def _git(*args: str, cwd: Path) -> tuple[bool, str]:
    code, detail = _run(*args, cwd=cwd)
    return code == 0, detail


def _remotes(repo: Path) -> tuple[bool, list[str]]:
    """The remote names, or (False, [git's error]) when git could not say. A git failure is not 'no origin'."""
    ok, out = _git("remote", cwd=repo)
    return ok, out.split() if ok else [out]


def push_branch(repo: Path, branch: str) -> tuple[bool, str]:
    """Fast-forward push of `branch` to origin under the same name; never forced."""
    ok, remotes = _remotes(repo)
    if not ok:
        return False, remotes[0]
    if "origin" not in remotes:
        return True, "no origin"
    return _git("push", "origin", f"refs/heads/{branch}:refs/heads/{branch}", cwd=repo)


def fetch_branch(repo: Path, branch: str) -> FetchOutcome:
    """Find `branch` locally or fetch it into refs/remotes/origin; no network call when it is local."""
    if _git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo)[0]:
        return FetchOutcome(Kind.PRESENT_LOCALLY)
    ok, remotes = _remotes(repo)
    if not ok:
        return FetchOutcome(Kind.FETCH_FAILED, remotes[0])
    if "origin" not in remotes:
        return FetchOutcome(Kind.MISSING_EVERYWHERE)
    code, detail = _run("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=repo)
    if code == 0:
        return FetchOutcome(Kind.FETCHED)
    if code == _TIMED_OUT:
        return FetchOutcome(Kind.FETCH_FAILED, detail)
    probe, _ = _run("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}", cwd=repo)
    if probe == _LS_REMOTE_NO_MATCH:
        return FetchOutcome(Kind.MISSING_EVERYWHERE)
    return FetchOutcome(Kind.FETCH_FAILED, detail)


def fetch_branch_tip(repo: Path, branch: str) -> FetchOutcome:
    """Refresh refs/remotes/origin/<branch> from origin; never short-circuits on a local branch, which is the stale copy."""
    ok, remotes = _remotes(repo)
    if not ok:
        return FetchOutcome(Kind.FETCH_FAILED, remotes[0])
    if "origin" not in remotes:
        return FetchOutcome(Kind.MISSING_EVERYWHERE)
    code, detail = _run("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=repo)
    if code == 0:
        return FetchOutcome(Kind.FETCHED)
    if code == _TIMED_OUT:
        return FetchOutcome(Kind.FETCH_FAILED, detail)
    probe, _ = _run("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}", cwd=repo)
    if probe == _LS_REMOTE_NO_MATCH:
        return FetchOutcome(Kind.MISSING_EVERYWHERE)
    return FetchOutcome(Kind.FETCH_FAILED, detail)


def missing_branch_cause(branch: str, outcome: FetchOutcome) -> str:
    """The one-line stop cause for a phase branch that could not be found."""
    if outcome.kind == Kind.FETCH_FAILED:
        first = outcome.detail.splitlines()[0] if outcome.detail else "no detail from git"
        return f"phase branch {branch} could not be fetched because origin was unreachable: {first}"
    return f"phase branch {branch} exists neither locally nor on origin"


def materialise_local(repo: Path, branch: str) -> tuple[bool, str]:
    """Create refs/heads/<branch> at origin/<branch>; refuses to move a branch that already exists."""
    return _git("branch", branch, f"refs/remotes/origin/{branch}", cwd=repo)
