"""Git edge for the base check: fetch origin, test whether the worktree holds a commit, merge missing commits in. Never pushes."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from harness.assembly import git


def conflict_cause(commit: str, files: Iterable[str]) -> str:
    listed = ", ".join(sorted(files)) or "none reported"
    return f"merging {commit} from the base conflicted in: {listed}"


def fetch_origin(repo: Path) -> tuple[bool, str]:
    """(True, note) when there is no origin; (False, detail) when the fetch fails."""
    code, remotes = git(repo, "remote")
    if code != 0:
        return False, f"could not list remotes in {repo}: {remotes}"
    if "origin" not in remotes.split():
        return True, "no origin remote, nothing to fetch"
    code, out = git(repo, "fetch", "origin")
    if code != 0:
        return False, f"fetch of origin failed: {out}"
    return True, "fetched origin"


def holds(worktree: Path, commit: str, ref: str = "HEAD") -> bool:
    """Ancestor of ref (default HEAD), or for a single-parent commit, its patch-id matches a commit reachable from ref."""
    if git(worktree, "cat-file", "-e", f"{commit}^{{commit}}")[0] != 0:
        return False
    if git(worktree, "merge-base", "--is-ancestor", commit, ref)[0] == 0:
        return True
    code, line = git(worktree, "rev-list", "--parents", "-n", "1", commit)
    if code != 0 or len(line.split()) != 2:
        return False
    code, cherry = git(worktree, "cherry", ref, commit, f"{commit}^")
    if code != 0:
        return False
    return any(row.startswith("-") for row in cherry.splitlines())


def merge_in(worktree: Path, commits: Iterable[str]) -> tuple[bool, str]:
    """Merge each commit HEAD does not hold, with a merge commit. A conflict aborts, leaves the worktree clean, and names the files."""
    for commit in sorted(set(commits)):
        if holds(worktree, commit):
            continue
        code, _ = git(worktree, "merge", "--no-ff", "--no-edit", commit)
        if code != 0:
            _, listing = git(worktree, "diff", "--name-only", "--diff-filter=U")
            git(worktree, "merge", "--abort")
            return False, conflict_cause(commit, listing.splitlines())
    return True, ""
