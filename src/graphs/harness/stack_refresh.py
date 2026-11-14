"""Merge the default ref (or a parent phase) into a level branch before the level builds. Never pushes, never chooses a side.

A refresh is always a merge, never a rebase: a phase branch's approved task merges are merge commits whose shas the
store records as `merged_commit`, and any rewrite of the branch (a plain rebase drops merges, `--rebase-merges`
re-creates them under new shas) leaves those recorded shas off the branch.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
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


def lost_merges(recorded: Mapping[str, str | None], reachable: Mapping[str, bool]) -> list[str]:
    """Task ids with a recorded commit that is not reachable. A task absent from `reachable` counts as lost."""
    return sorted(task for task, commit in recorded.items() if commit and not reachable.get(task, False))


def rebase_lost_reason(branch: str, onto: str, lost: Iterable[str]) -> str:
    listed = ", ".join(sorted(lost))
    return f"rebasing {branch} onto {onto} lost the merged commits of: {listed}; the branch was left as it was"


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


def unreachable(worktree: Path, commits: Iterable[str], ref: str) -> tuple[str, ...]:
    """Each of `commits` that is not an ancestor of (or equal to) `ref`, in the order given."""
    return tuple(c for c in commits if git(worktree, "merge-base", "--is-ancestor", c, ref)[0] != 0)


def dropped_reason(branch: str, onto: str, lost: Iterable[str]) -> str:
    listed = ", ".join(c[:12] for c in lost)
    return f"refreshing {branch} onto {onto} would drop recorded task merge(s) {listed}; {branch} was left as it was"


def guarded_refresh(
    worktree: Path,
    branch: str,
    onto: str,
    recorded: Iterable[str] = (),
    refresh: Callable[[Path, str, str], RefreshResult] = refresh_branch,
) -> RefreshResult:
    """`refresh` `onto` into `branch`, refusing when it would leave a recorded merged commit off the branch.

    Only the recorded commits the branch holds before the refresh are guarded; one already missing is not this
    refresh's doing. A refusal puts `branch` back at its old head, so nothing rewritten survives or is pushed.
    """
    code, before = git(worktree, "rev-parse", "HEAD")
    if code != 0:
        return _failed("", f"could not read the head of {branch}: {before}")
    held = [c for c in dict.fromkeys(recorded) if c and not unreachable(worktree, [c], before)]
    result = refresh(worktree, branch, onto)
    if not result.ok:
        return result
    lost = unreachable(worktree, held, "HEAD")
    if not lost:
        return result
    git(worktree, "reset", "--hard", "-q", before)
    return _failed(before, dropped_reason(branch, onto, lost))
