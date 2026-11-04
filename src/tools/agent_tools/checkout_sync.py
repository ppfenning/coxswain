"""Decide whether the chair may fast-forward its checkout of a repository to `origin/<default>`, and do it.

`decide` and `report` are pure. `sync_checkout` is the edge: it reads the facts through a runner, and runs
`git merge --ff-only` only for the fast_forward verdict. It never fetches: the caller owns a fresh origin ref.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

Verdict = Literal["fast_forward", "already_current", "dirty", "wrong_branch", "diverged"]
Runner = Callable[[Path, Sequence[str]], tuple[int, str]]  # (repo, git args) -> (exit code, stdout)

GIT_TIMEOUT_SECONDS = 60
_UNRUNNABLE_CODE = 127


def decide(clean: bool, branch: str | None, default: str, head_is_ancestor: bool, at_tip: bool) -> Verdict:
    """`head_is_ancestor` includes equality; `at_tip` is HEAD equal to origin's tip and is what tells already_current from fast_forward."""
    if not clean:
        return "dirty"
    if branch != default:
        return "wrong_branch"
    if at_tip:
        return "already_current"
    if head_is_ancestor:
        return "fast_forward"
    return "diverged"


def report(verdict: Verdict, branch: str | None, default: str) -> str | None:
    """A one-line reason for a checkout left alone; None when the checkout was or is current."""
    if verdict == "dirty":
        return f"checkout not fast-forwarded: working tree has uncommitted changes (branch {branch or 'detached'})"
    if verdict == "wrong_branch":
        return f"checkout not fast-forwarded: on {branch or 'a detached HEAD'}, not {default}"
    if verdict == "diverged":
        return f"checkout not fast-forwarded: {default} has local commits that origin/{default} lacks"
    return None


def git_runner(repo: Path, args: Sequence[str]) -> tuple[int, str]:
    """Edge. Run git in `repo`; a timeout or an unrunnable git becomes a nonzero code with the reason as output."""
    try:
        done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return _UNRUNNABLE_CODE, f"git timed out after {GIT_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return _UNRUNNABLE_CODE, f"cannot run git: {exc}"
    return done.returncode, done.stdout if done.returncode == 0 else done.stderr or done.stdout


def sync_checkout(repo: Path, default: str, run: Runner = git_runner) -> tuple[Verdict, str | None]:
    """Edge. Read the five facts, decide, and fast-forward only on fast_forward.

    An unreadable status counts as dirty, and an unreadable origin ref counts as not an ancestor, so a failed
    read leaves the checkout alone. A fast-forward git refuses is reported, never retried another way.
    """
    origin = f"origin/{default}"
    status_code, status = run(repo, ["status", "--porcelain"])
    branch_code, branch_out = run(repo, ["symbolic-ref", "--short", "-q", "HEAD"])
    head_code, head = run(repo, ["rev-parse", "HEAD"])
    tip_code, tip = run(repo, ["rev-parse", origin])
    ancestor_code, _ = run(repo, ["merge-base", "--is-ancestor", "HEAD", origin])
    branch = branch_out.strip() if branch_code == 0 and branch_out.strip() else None
    verdict = decide(
        clean=status_code == 0 and not status.strip(),
        branch=branch,
        default=default,
        head_is_ancestor=ancestor_code == 0,
        at_tip=head_code == 0 and tip_code == 0 and head.strip() == tip.strip(),
    )
    if verdict != "fast_forward":
        return verdict, report(verdict, branch, default)
    merge_code, merge_out = run(repo, ["merge", "--ff-only", origin])
    if merge_code == 0:
        return verdict, None
    reason = next((line.strip() for line in merge_out.splitlines() if line.strip()), f"exit code {merge_code}")
    return verdict, f"checkout fast-forward to {origin} failed: {reason}"
