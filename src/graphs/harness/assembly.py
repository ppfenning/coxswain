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


RERERE_OFF_NOTE = " (rerere resolution could not be applied; conflict found with rerere off)"


def failure_cause(parent: str, previous: str, files: Iterable[str], *, output: str = "", rerere_off: bool = False) -> str:
    """`output` is git's own text, named when no file is unmerged; `rerere_off` marks a conflict found with rerere disabled."""
    listed = ", ".join(sorted(files)) or output or "none reported"
    return f"merging {parent} after {previous} conflicted in: {listed}{RERERE_OFF_NOTE if rerere_off else ''}"


def git(worktree: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", *IDENTITY, "-C", str(worktree), *args], capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def _unmerged(worktree: Path) -> list[str]:
    return git(worktree, "diff", "--name-only", "--diff-filter=U")[1].splitlines()


def _merging(worktree: Path) -> bool:
    return git(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0


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
            files = _unmerged(worktree)
            if files or not _merging(worktree):
                git(worktree, "merge", "--abort")
                return AssemblyResult(False, "", failure_cause(parent, previous, files, output=out))
            # rerere staged a recorded resolution and left the merge unfinished: commit it.
            if git(worktree, "commit", "--no-edit")[0] != 0:
                git(worktree, "merge", "--abort")
                code, out = git(worktree, "-c", "rerere.enabled=false", "merge", "--no-ff", "--no-edit", parent)
                files = _unmerged(worktree)
                git(worktree, "merge", "--abort")
                if code != 0:
                    return AssemblyResult(False, "", failure_cause(parent, previous, files, output=out, rerere_off=True))
        previous = parent
    code, head = git(worktree, "rev-parse", "HEAD")
    if code != 0:
        return AssemblyResult(False, "", f"could not read the head of {target_branch}: {head}")
    return AssemblyResult(True, head, "")
