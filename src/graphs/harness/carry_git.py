"""Git edge for a carry: test where an earlier run's commit lives, fetch it, cherry-pick it. Never pushes, never touches the store."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from harness.assembly import git

# A squash or rebase merge puts the number in the subject as "(#N)"; a merge commit says "Merge pull request #N".
PULL_REQUEST_SUBJECT = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)")


def conflict_cause(commit: str, files: Iterable[str], detail: str = "") -> str:
    listed = ", ".join(sorted(files))
    return f"cherry-picking {commit} conflicted in: {listed}" if listed else f"cherry-picking {commit} failed: {detail}"


def has_commit(repo: Path, commit: str) -> bool:
    return git(repo, "cat-file", "-e", f"{commit}^{{commit}}")[0] == 0


def on_branch(repo: Path, commit: str, branch: str) -> bool:
    """The commit is an ancestor of the branch. An unknown commit or branch is False."""
    return has_commit(repo, commit) and git(repo, "merge-base", "--is-ancestor", commit, branch)[0] == 0


def patch_ids(repo: Path, log_patches: str) -> dict[str, str]:
    """Map commit id to stable patch-id for `git log -p` text. Commits with an empty patch are absent."""
    result = subprocess.run(
        ["git", "-C", str(repo), "patch-id", "--stable"], input=log_patches, capture_output=True, text=True
    )
    pairs = (line.split() for line in result.stdout.splitlines())
    return {pair[1]: pair[0] for pair in pairs if len(pair) == 2}


def pull_request_number(subject: str) -> int | None:
    match = PULL_REQUEST_SUBJECT.search(subject)
    return int(match.group(1) or match.group(2)) if match else None


def landed_on_base(repo: Path, commit: str, base_ref: str) -> int | None:
    """Pull request number of a base commit carrying the same patch-id as `commit`, or None. A merge commit is never matched."""
    code, line = git(repo, "rev-list", "--parents", "-n", "1", commit)
    if code != 0 or len(line.split()) != 2:
        return None
    full = line.split()[0]
    code, cherry = git(repo, "cherry", base_ref, full, f"{full}^")
    if code != 0 or not any(row.startswith("-") for row in cherry.splitlines()):
        return None
    code, mine = git(repo, "log", "-p", "--no-merges", "-n", "1", "--format=commit %H", full)
    code_base, base = git(repo, "log", "-p", "--no-merges", "--format=commit %H", f"{full}^..{base_ref}")
    if code != 0 or code_base != 0:
        return None
    ids = patch_ids(repo, f"{mine}\n{base}\n")
    if full not in ids:
        return None
    for sha in (sha for sha, pid in ids.items() if pid == ids[full] and sha != full):
        code, subject = git(repo, "log", "-n", "1", "--format=%s", sha)
        number = pull_request_number(subject) if code == 0 else None
        if number is not None:
            return number
    return None


def fetch_commit(repo: Path, commit: str, run_namespace: str, sources: Iterable[str]) -> bool:
    """True once the commit object is present locally. Tries origin, the run's branch namespace on origin, then each source."""
    if has_commit(repo, commit):
        return True
    attempts = (
        ("origin", commit),
        ("origin", f"+refs/heads/{run_namespace}/*:refs/remotes/origin/{run_namespace}/*"),
        *((source, commit) for source in sources),
    )
    return any(git(repo, "fetch", remote, ref)[0] == 0 and has_commit(repo, commit) for remote, ref in attempts)


def cherry_pick(worktree: Path, commit: str) -> tuple[bool, str]:
    """(True, new head) on success; (False, cause naming the conflicting files) after an abort that leaves the worktree clean."""
    code, out = git(worktree, "cherry-pick", commit)
    if code == 0:
        return True, git(worktree, "rev-parse", "HEAD")[1]
    _, listing = git(worktree, "diff", "--name-only", "--diff-filter=U")
    git(worktree, "cherry-pick", "--abort")
    return False, conflict_cause(commit, listing.splitlines(), out)
