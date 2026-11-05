"""Merge the default ref into a level branch before the level builds. Never pushes, never chooses a side."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from harness.assembly import git


@dataclass(frozen=True)
class RefreshResult:
    """`refreshed` is true only when a merge commit was made. `paths` and `reason` are empty on success."""

    ok: bool
    refreshed: bool
    head_before: str
    head_after: str
    paths: tuple[str, ...]
    reason: str


def refresh_record(branch: str, default_ref: str, head_before: str, head_after: str) -> str:
    return f"refreshed {branch} onto {default_ref}: head {head_before} became {head_after}"


def refresh_reason(branch: str, default_ref: str, paths: Iterable[str]) -> str:
    listed = ", ".join(sorted(paths)) or "none reported"
    return f"merging {default_ref} into {branch} conflicted in: {listed}; the level was not built on the stale tree"


def _failed(head: str, reason: str, paths: tuple[str, ...] = ()) -> RefreshResult:
    return RefreshResult(False, False, head, head, paths, reason)


def refresh_branch(worktree: Path, branch: str, default_ref: str) -> RefreshResult:
    """Merge `default_ref` into `branch`, which must be checked out in `worktree`.

    A conflict aborts the merge, leaves the worktree clean, and reports the conflicting paths.
    """
    code, current = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return _failed("", f"could not read the checked-out branch in {worktree}: {current}")
    code, before = git(worktree, "rev-parse", "HEAD")
    if code != 0:
        return _failed("", f"could not read the head of {branch}: {before}")
    if current != branch:
        return _failed(before, f"{worktree} has {current} checked out, not {branch}")
    if git(worktree, "merge-base", "--is-ancestor", default_ref, "HEAD")[0] == 0:
        return RefreshResult(True, False, before, before, (), "")
    code, out = git(worktree, "merge", "--no-ff", "--no-edit", default_ref)
    if code != 0:
        _, listing = git(worktree, "diff", "--name-only", "--diff-filter=U")
        git(worktree, "merge", "--abort")
        paths = tuple(sorted(listing.splitlines()))
        return _failed(before, refresh_reason(branch, default_ref, paths), paths)
    code, after = git(worktree, "rev-parse", "HEAD")
    if code != 0:
        return _failed(before, f"could not read the head of {branch} after the merge: {after}")
    return RefreshResult(True, True, before, after, (), "")
