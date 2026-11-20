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
    PRESENT_LOCALLY = "present_locally"  # local and not behind origin (or origin could not be asked): kept as it is
    FAST_FORWARDED = "fast_forwarded"  # local was behind origin and now holds origin's tip
    DIVERGED = "diverged"  # local and origin each hold commits the other lacks: local kept, origin's tip on a side ref
    DIVERGED_CONTENT = "diverged_content"  # diverged, and the two tips hold different trees: only a person decides
    RESET = "reset_to_origin"  # diverged with the same tree as origin's tip: local was put at origin's tip (`settle_diverged`)
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


FETCHED_PREFIX = "refs/coxswain/fetched/origin"


def fetched_ref(branch: str) -> str:
    """Where origin's tip of a phase branch is kept when it diverged from the local branch."""
    return f"{FETCHED_PREFIX}/{branch}"


def refresh_verdict(local_in_origin: bool, origin_in_local: bool) -> Kind:
    """Pure. What origin's tip does to an existing local phase branch; it only ever moves the branch forward.

    `local_in_origin` is "local is an ancestor of origin", `origin_in_local` the reverse; equal tips are both, and keep."""
    if origin_in_local:
        return Kind.PRESENT_LOCALLY
    return Kind.FAST_FORWARDED if local_in_origin else Kind.DIVERGED


def _checked_out_at(repo: Path, branch: str) -> Path | None:
    """The worktree that has `branch` checked out, if any; update-ref there would leave its files behind the ref."""
    ok, out = _git("worktree", "list", "--porcelain", cwd=repo)
    if not ok:
        return None
    path: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def _refresh_local(repo: Path, branch: str) -> FetchOutcome:
    """Bring an existing local `branch` up to origin's tip by fast-forward only; never moves it backwards or sideways.

    Origin that cannot be asked, or holds no such branch, leaves the local branch as it is (PRESENT_LOCALLY): the
    local copy is still something to build on, which is what the build used before this refresh existed."""
    ok, remotes = _remotes(repo)
    if not ok or "origin" not in remotes:
        return FetchOutcome(Kind.PRESENT_LOCALLY)
    remote_ref = f"refs/remotes/origin/{branch}"
    code, detail = _run("fetch", "origin", f"+refs/heads/{branch}:{remote_ref}", cwd=repo)
    if code != 0:
        return FetchOutcome(Kind.PRESENT_LOCALLY, f"not refreshed from origin: {detail.splitlines()[0] if detail else code}")
    local_ok, local = _git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", cwd=repo)
    origin_ok, origin = _git("rev-parse", "--verify", f"{remote_ref}^{{commit}}", cwd=repo)
    if not (local_ok and origin_ok):
        return FetchOutcome(Kind.PRESENT_LOCALLY, "not refreshed from origin: a tip could not be read")
    verdict = refresh_verdict(
        _git("merge-base", "--is-ancestor", local, origin, cwd=repo)[0],
        _git("merge-base", "--is-ancestor", origin, local, cwd=repo)[0],
    )
    if verdict == Kind.PRESENT_LOCALLY:
        return FetchOutcome(Kind.PRESENT_LOCALLY)
    if verdict == Kind.DIVERGED:
        side = fetched_ref(branch)
        _git("update-ref", "-m", "phase fetch: diverged from origin", side, origin, cwd=repo)
        return FetchOutcome(
            Kind.DIVERGED, f"{branch}: kept local {local[:12]}; origin's {origin[:12]} diverged and is saved at {side}"
        )
    worktree = _checked_out_at(repo, branch)
    if worktree is not None:
        # A checked-out branch moves with its files, and only when that worktree is clean enough to fast-forward.
        moved, out = _git("merge", "--ff-only", "-q", origin, cwd=worktree)
    else:
        moved, out = _git("update-ref", "-m", "phase fetch: fast-forward from origin", f"refs/heads/{branch}", origin, local, cwd=repo)
    if not moved:
        return FetchOutcome(Kind.PRESENT_LOCALLY, f"{branch} is behind origin's {origin[:12]} but could not fast-forward: {out}")
    return FetchOutcome(Kind.FAST_FORWARDED, f"{branch}: fast-forwarded {local[:12]} to origin's {origin[:12]}")


# Wrong belief (2026-11-11): "a local phase branch is current, so no network call is needed". Origin moved ahead with a
# repaired merge, the host kept building on its stale copy, and the run quarantined with "merged commit … is not on
# epic/…". A local branch is now refreshed from origin, forward only.
def fetch_branch(repo: Path, branch: str) -> FetchOutcome:
    """Find `branch` locally, refreshed from origin forward only, or fetch it into refs/remotes/origin."""
    if _git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo)[0]:
        return _refresh_local(repo, branch)
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


def diverged_action(local_tree: str, origin_tree: str) -> str:
    """Pure. What a local initiative branch that diverged from origin's copy does: `reset` to origin's tip when both
    tips hold the same tree (two merge commits of the same work: nothing of the local one is lost), else `alarm`,
    since only a person can say which work is the right one. A tree that could not be read is never the same tree."""
    return "reset" if local_tree and local_tree == origin_tree else "alarm"


def settle_diverged(repo: Path, branch: str) -> FetchOutcome:
    """Edge. After a fetch said `branch` diverged from origin (origin's tip is at `fetched_ref`): put the local branch
    at origin's tip when the trees are the same (RESET); DIVERGED_CONTENT, naming both commits, when they differ; else
    DIVERGED (a tip that could not be read, a checked-out branch, a reset git refused).

    Only the lander merges into an initiative branch, so a local copy that diverged with the same tree is a second
    merge commit of work origin already has. The move is a compare-and-swap on the local tip, and a branch checked out
    in a worktree is never moved under its files."""
    side = fetched_ref(branch)
    local_ok, local = _git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", cwd=repo)
    origin_ok, origin = _git("rev-parse", "--verify", f"{side}^{{commit}}", cwd=repo)
    if not (local_ok and origin_ok):
        return FetchOutcome(Kind.DIVERGED, f"{branch}: diverged from origin, and a tip could not be read to compare")
    trees = [_git("rev-parse", "--verify", f"{sha}^{{tree}}", cwd=repo) for sha in (local, origin)]
    local_tree, origin_tree = (out if ok else "" for ok, out in trees)
    if diverged_action(local_tree, origin_tree) == "alarm":
        return FetchOutcome(Kind.DIVERGED_CONTENT, (
            f"needs the chair: {branch} has diverged from origin with different content: this clone's {local[:12]} "
            f"and origin's {origin[:12]} hold different trees, so neither is moved"))
    if _checked_out_at(repo, branch) is not None:
        return FetchOutcome(Kind.DIVERGED, f"{branch}: this clone's {local[:12]} holds origin's {origin[:12]} tree, "
                                           "but it is checked out in a worktree, so it is not reset here")
    moved, out = _git("update-ref", "-m", "initiative fetch: same tree as origin, reset to it",
                      f"refs/heads/{branch}", origin, local, cwd=repo)
    if not moved:
        return FetchOutcome(Kind.DIVERGED, f"{branch}: could not be reset to origin's {origin[:12]}: {out}")
    return FetchOutcome(Kind.RESET, f"{branch}: this clone's {local[:12]} held the same tree as origin's {origin[:12]}; reset to origin's")


def missing_branch_cause(branch: str, outcome: FetchOutcome) -> str:
    """The one-line stop cause for a phase branch that could not be found."""
    if outcome.kind == Kind.FETCH_FAILED:
        first = outcome.detail.splitlines()[0] if outcome.detail else "no detail from git"
        return f"phase branch {branch} could not be fetched because origin was unreachable: {first}"
    return f"phase branch {branch} exists neither locally nor on origin"


def materialise_local(repo: Path, branch: str) -> tuple[bool, str]:
    """Create refs/heads/<branch> at origin/<branch>; refuses to move a branch that already exists."""
    return _git("branch", branch, f"refs/remotes/origin/{branch}", cwd=repo)
