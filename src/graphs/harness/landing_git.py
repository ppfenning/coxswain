"""Git for the landing model: the initiative branch, the phase merge into it, and its refresh onto the parent.

One rule set at every level: a refresh is a forward merge of the parent into the child, and nothing here rebases,
resets or force-moves a branch the landing record names. Every merge is built on a detached scratch worktree, so no
working checkout is touched, and the branch is moved last, with a compare-and-swap on its old tip.

The guard: a branch is never moved to a tip that lacks a sha the record says is on it.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Mechanical commits: the lander joins branches, it never authors.
IDENTITY = ("-c", "user.email=lander@invalid", "-c", "user.name=lander")

Checks = Callable[[Path], tuple[bool, str]]


@dataclass(frozen=True)
class Merge:
    """The outcome of one merge onto a branch. `status` is merged, up_to_date, conflict, checks_failed, guard or error."""

    status: str
    tip: str = ""  # the branch's tip afterwards (unchanged unless merged)
    merged: str = ""  # the sha that was merged in
    paths: tuple[str, ...] = field(default_factory=tuple)  # conflicted paths
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("merged", "up_to_date")


def git(repo: Path | str, *args: str) -> tuple[bool, str]:
    """Run git in `repo`. (ok, stdout stripped, or stderr when it failed)."""
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.returncode == 0, (done.stdout if done.returncode == 0 else done.stderr or done.stdout).strip()


def rev(repo: Path | str, ref: str) -> str | None:
    ok, out = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out if ok and out else None


def branch_ref(repo: Path | str, branch: str) -> str | None:
    """`branch` as a ref git can read: the local branch, else its copy on origin, else None."""
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        if rev(repo, ref):
            return ref
    return None


def is_ancestor(repo: Path | str, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer)[0]


def missing_from(repo: Path | str, tip: str, shas: Sequence[str]) -> list[str]:
    """The recorded shas that `tip` does not hold: what a move to `tip` would drop."""
    return [sha for sha in shas if sha and not is_ancestor(repo, sha, tip)]


def ensure_branch(repo: Path | str, branch: str, parent_ref: str) -> tuple[str | None, bool, str]:
    """Cut `branch` at `parent_ref` when it does not exist. (its tip, whether this call created it, why not).

    A branch that exists only on origin is brought local at that tip. An existing branch is never moved."""
    local = rev(repo, f"refs/heads/{branch}")
    if local:
        return local, False, ""
    remote = rev(repo, f"refs/remotes/origin/{branch}")
    start = remote or rev(repo, parent_ref)
    if start is None:
        return None, False, f"cannot cut {branch}: {parent_ref} is not a commit"
    ok, out = git(repo, "branch", "--no-track", branch, start)
    if not ok:
        return None, False, f"cannot cut {branch}: {out}"
    return start, remote is None, ""


def _scratch(repo: Path | str, at: str) -> tuple[Path | None, str]:
    path = Path(tempfile.mkdtemp(prefix="lander-"))
    ok, out = git(repo, "worktree", "add", "--detach", str(path), at)
    return (path, "") if ok else (None, out)


def _drop(repo: Path | str, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path))


def _move(repo: Path | str, branch: str, new: str, old: str) -> tuple[bool, str]:
    """Move a local branch from `old` to `new`, and only then: the compare-and-swap that makes two mergers safe."""
    return git(repo, "update-ref", "-m", "lander: merge", f"refs/heads/{branch}", new, old)


def merge_into(repo: Path | str, *, branch: str, source: str, message: str, recorded: Sequence[str] = (),
               checks: Checks | None = None) -> Merge:
    """Merge `source` into local `branch` with a merge commit, on a scratch worktree, then move the branch.

    `source` already on the branch is `up_to_date`. A conflict aborts and names the paths. `checks` runs on the merged
    tree before the branch moves; a failure leaves the branch where it was. `recorded` are the shas the landing
    record says the branch holds: a merge that would leave one out is refused (`guard`) and the branch is unchanged."""
    old = rev(repo, f"refs/heads/{branch}")
    incoming = rev(repo, source)
    if old is None:
        return Merge("error", detail=f"no local branch {branch}")
    if incoming is None:
        return Merge("error", tip=old, detail=f"{source} is not a commit")
    if is_ancestor(repo, incoming, old):
        return Merge("up_to_date", tip=old, merged=incoming)
    scratch, why = _scratch(repo, old)
    if scratch is None:
        return Merge("error", tip=old, merged=incoming, detail=why)
    try:
        ok, out = git(scratch, *IDENTITY, "merge", "--no-ff", "--no-edit", "-m", message, incoming)
        if not ok:
            _, unmerged = git(scratch, "diff", "--name-only", "--diff-filter=U")
            paths = tuple(p for p in unmerged.splitlines() if p)
            git(scratch, "merge", "--abort")
            if paths:
                return Merge("conflict", tip=old, merged=incoming, paths=paths, detail=f"conflict in {', '.join(paths)}")
            return Merge("error", tip=old, merged=incoming, detail=out)
        new = rev(scratch, "HEAD")
        if new is None:
            return Merge("error", tip=old, merged=incoming, detail="the merge left no commit")
        dropped = missing_from(repo, new, recorded)
        if dropped:
            return Merge("guard", tip=old, merged=incoming,
                         detail=f"refused: the merge would drop recorded commit(s) {', '.join(s[:12] for s in dropped)}")
        if checks is not None:
            passed, output = checks(scratch)
            if not passed:
                return Merge("checks_failed", tip=old, merged=incoming, detail=output)
        moved, out = _move(repo, branch, new, old)
        if not moved:
            return Merge("error", tip=old, merged=incoming, detail=f"{branch} moved while it was being merged: {out}")
        return Merge("merged", tip=new, merged=incoming)
    finally:
        _drop(repo, scratch)


def conflict_commit(repo: Path | str, *, branch: str, source: str, message: str) -> Merge:
    """Commit the conflicted merge of `source` into local `branch`, conflict markers and all, and move the branch.

    This is the base a rectify task builds on: its builder returns a patch, so the conflict has to be in the tree it
    starts from. The branch then holds `source`, so once the markers are resolved the merge the other way is clean."""
    old = rev(repo, f"refs/heads/{branch}")
    incoming = rev(repo, source)
    if old is None or incoming is None:
        return Merge("error", tip=old or "", detail=f"no local branch {branch}" if old is None else f"{source} is not a commit")
    scratch, why = _scratch(repo, old)
    if scratch is None:
        return Merge("error", tip=old, merged=incoming, detail=why)
    try:
        ok, _ = git(scratch, *IDENTITY, "merge", "--no-ff", "--no-edit", "-m", message, incoming)
        _, unmerged = git(scratch, "diff", "--name-only", "--diff-filter=U")
        paths = tuple(p for p in unmerged.splitlines() if p)
        if not ok:
            added, out = git(scratch, "add", "--all")
            committed, out = git(scratch, *IDENTITY, "commit", "--no-verify", "-m", message) if added else (False, out)
            if not committed:
                git(scratch, "merge", "--abort")
                return Merge("error", tip=old, merged=incoming, detail=out)
        new = rev(scratch, "HEAD")
        if new is None or new == old:
            return Merge("up_to_date", tip=old, merged=incoming)
        moved, out = _move(repo, branch, new, old)
        if not moved:
            return Merge("error", tip=old, merged=incoming, detail=out)
        return Merge("conflict" if paths else "merged", tip=new, merged=incoming, paths=paths)
    finally:
        _drop(repo, scratch)


def ahead_behind(repo: Path | str, branch: str, parent_ref: str) -> tuple[int, int] | None:
    """(commits `branch` has over the parent, commits it lacks), None when either is not a commit."""
    ok, out = git(repo, "rev-list", "--left-right", "--count", f"{branch}...{parent_ref}")
    parts = out.split()
    return (int(parts[0]), int(parts[1])) if ok and len(parts) == 2 else None


def safe_to_delete(repo: Path | str, branch: str, kept_tip: str, recorded: Sequence[str]) -> list[str]:
    """The recorded shas only `branch` still holds: deleting it would lose them. Empty means it may go."""
    tip = rev(repo, f"refs/heads/{branch}")
    if tip is None:
        return []
    return [sha for sha in recorded if sha and is_ancestor(repo, sha, tip) and not is_ancestor(repo, sha, kept_tip)]


def on_scratch(repo: Path | str, at: str, check: Checks) -> tuple[bool, str]:
    """Run `check` on a detached scratch worktree of `at`, and remove the worktree whatever happens."""
    scratch, why = _scratch(repo, at)
    if scratch is None:
        return False, why
    try:
        return check(scratch)
    finally:
        _drop(repo, scratch)
