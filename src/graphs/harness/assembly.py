"""Assemble a branch locally by merging several parent branches in a fixed order. Never pushes."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

IDENTITY = ("-c", "user.name=coxswain", "-c", "user.email=coxswain@localhost")


@dataclass(frozen=True)
class AssemblyResult:
    """`head` is empty on failure; `cause` is empty on success."""

    ok: bool
    head: str
    cause: str


def assembly_order(branches: Iterable[str]) -> tuple[str, ...]:
    """Sorted unique branch names, so two runs assemble the same way."""
    return tuple(sorted(set(branches)))


def failure_cause(parent: str, previous: str, files: Iterable[str]) -> str:
    listed = ", ".join(sorted(files)) or "none reported"
    return f"merging {parent} after {previous} conflicted in: {listed}"


def git(worktree: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", *IDENTITY, "-C", str(worktree), *args], capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def assemble(repo: Path, worktree: Path, base_ref: str, parents: Iterable[str], target_branch: str) -> AssemblyResult:
    """Check out `target_branch` at `base_ref` in an existing `worktree` of `repo`, then merge each parent with a merge commit.

    A conflict aborts the merge, leaves the worktree clean, and reports the cause. No side is ever chosen.
    """
    code, out = git(worktree, "checkout", "-B", target_branch, base_ref)
    if code != 0:
        return AssemblyResult(False, "", f"could not create {target_branch} at {base_ref} in {repo}: {out}")
    previous = base_ref
    for parent in assembly_order(parents):
        if git(worktree, "merge-base", "--is-ancestor", parent, "HEAD")[0] == 0:
            continue
        code, out = git(worktree, "merge", "--no-ff", "--no-edit", parent)
        if code != 0:
            _, listing = git(worktree, "diff", "--name-only", "--diff-filter=U")
            git(worktree, "merge", "--abort")
            return AssemblyResult(False, "", failure_cause(parent, previous, listing.splitlines()))
        previous = parent
    code, head = git(worktree, "rev-parse", "HEAD")
    if code != 0:
        return AssemblyResult(False, "", f"could not read the head of {target_branch}: {head}")
    return AssemblyResult(True, head, "")
