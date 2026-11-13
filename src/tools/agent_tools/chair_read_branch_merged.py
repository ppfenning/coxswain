"""Whether an approved commit is already on a phase or pr branch, by sha or by the same patch."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable

__all__ = ["branch_merged", "is_merged", "patch_id"]

GitRun = Callable[[list[str]], tuple[int, str]]  # argv from `-C` onward -> (exit code, output); the edge's only door to git


def patch_id(diff: str) -> str:
    """Hash of a diff's file headers and changed lines, blind to index lines, hunk positions and whitespace; "" for an empty diff."""
    kept = [
        "".join(line.split())
        for line in diff.splitlines()
        if line.startswith(("diff --git", "+", "-"))
    ]
    return hashlib.sha1("\n".join(kept).encode()).hexdigest() if kept else ""


def is_merged(sha: str, commit_patch_id: str, on_branch: Iterable[tuple[str, str]]) -> bool:
    """True when `sha` is on the branch or a branch commit has the same patch id; an empty patch id never matches."""
    return any(sha == s or (bool(commit_patch_id) and commit_patch_id == p) for s, p in on_branch)


def _patch_id_of(repo_root: str, sha: str, run: GitRun) -> str | None:
    """The patch id of one commit; None when git refuses it."""
    code, out = run(["-C", repo_root, "show", "--format=", "--no-color", "--no-ext-diff", "-U0", sha])
    return patch_id(out) if code == 0 else None


def _branch_commits(repo_root: str, branch: str, run: GitRun) -> list[tuple[str, str]] | None:
    """(sha, patch id) of each commit on `branch` that main lacks; None when the branch is missing or git fails."""
    code, out = run(["-C", repo_root, "rev-list", f"main..{branch}"])
    if code != 0:
        return None
    shas = out.split()
    ids = [_patch_id_of(repo_root, s, run) for s in shas]
    return None if None in ids else [(s, i) for s, i in zip(shas, ids) if i is not None]


def branch_merged(runner: GitRun, repo_root: str, commit: str, branches: Iterable[str]) -> bool:
    """Edge: True when `commit` is on any named branch, or a commit there carries the same patch. Failures are False."""
    if not commit or not repo_root:
        return False
    own = _patch_id_of(repo_root, commit, runner)
    if own is None:
        return False
    return any(
        is_merged(commit, own, found)
        for found in (_branch_commits(repo_root, b, runner) for b in branches)
        if found is not None
    )
