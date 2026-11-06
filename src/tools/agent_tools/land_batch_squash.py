"""Edge. Squash one source branch onto a shared batch branch, one attributed commit per source.

The first source of a batch builds the batch branch in its own land worktree; later sources reuse it."""

import shutil
import subprocess
from pathlib import Path


def _git(wt: Path | str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True)


def _out(r: subprocess.CompletedProcess) -> str:
    return r.stderr.strip() or r.stdout.strip()


def squash_onto_batch(repo: Path, step: dict) -> tuple[bool, str]:
    """`step`: branch, onto, from, subject, source, first. A refused call leaves the batch as it found it;
    a refused first call leaves neither worktree nor branch."""
    from agent_tools import cli  # cli will import this module, so a top-level import would be circular

    branch, onto, base = step["branch"], step["onto"], step["from"]
    wt = cli._land_worktree(repo, onto)
    if step["first"]:
        cli._remove_land_worktree(repo, onto)
        add = _git(repo, "worktree", "add", "-b", onto, str(wt), base)
        if add.returncode != 0:
            return False, _out(add)
        cli._link_venv(repo, wt)
    elif not wt.is_dir():
        return False, f"batch worktree missing: {wt}"

    before = _git(wt, "rev-parse", "HEAD").stdout.strip()

    def refuse(detail: str) -> tuple[bool, str]:
        # A squash merge leaves no MERGE_HEAD, so `merge --abort` has nothing to abort: reset to the head from before.
        _git(wt, "reset", "--hard", before)
        if step["first"]:
            _git(repo, "worktree", "remove", "--force", str(wt))
            shutil.rmtree(wt, ignore_errors=True)
            _git(repo, "worktree", "prune")
            _git(repo, "branch", "-D", onto)
        return False, detail

    author = _git(repo, "log", "-1", "--format=%an <%ae>", branch).stdout.strip()
    merge = _git(wt, "-c", "merge.conflictStyle=diff3", "merge", "--squash", branch)
    if merge.returncode != 0:
        conflicted = _git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
        # Does not reproduce squash_phase's scratch-merge for a parent phase that already squash-landed: the conflict is reported and the source goes back to its owner.
        return refuse(f"squash of {branch} conflicts in: {', '.join(conflicted) or _out(merge)}")
    if _git(wt, "diff", "--cached", "--quiet").returncode == 0:
        return True, f"nothing to squash: {branch} adds nothing over {base}"
    commit = _git(wt, "commit", "-q", "--author", author, "-m", step["subject"], "-m", f"Source: {step['source']}")
    if commit.returncode != 0:
        return refuse(_out(commit))
    return True, f"squashed {branch} onto {onto}"
