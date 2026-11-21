"""The phase lane's git edge: where the phase branch stands on origin, and one task's roll-up onto it.

A task rolls up as exactly one commit on the phase branch, linear, never a merge: its change (the task worktree's tree
against the tip it was cut from) is applied cleanly onto the phase tip and committed with the `Cox-*` trailers
(`message`, plan §1.4), then pushed. The push is a plain fast-forward, never forced, so of two lanes that both think
they hold a phase the second push is refused. A change that does not apply cleanly is reported as such: resolving it
is the merge agent's (L5), not this module's.

The phase worktree is detached at the tip and pushed as `HEAD:refs/heads/<branch>`, so no local branch of the
repository is created, moved or checked out. Origin's refs are fetched to refs private to the run, so two lanes on one
checkout never read each other's fetch. `phase_lock` is the phase's local lock: every lane on this machine that rolls
up into one phase takes it, each thread of a lane too, from the apply to the push.

`message` and `branch_name` are pure; everything else is an edge.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harness.epic_branches import IDENTITY
from harness.lane_progress import LOG_ARGS
from harness.worktree import registry_lock

__all__ = ["apply_and_commit", "branch_name", "change", "fetch", "head", "log_since", "message", "open_phase_worktree",
           "phase_lock", "push", "reset_to"]

_TIMEOUT_S = 120
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def message(initiative: str, phase: str, task: str, run_id: str, title: str, kind: str = "task", round_: int = 0,
            merged_with: str = "") -> str:
    """The roll-up commit's message: today's subject (`epic <run_id>: <task>`, which the lander and the branch readers
    already parse), the task's title, then the trailers. `Cox-Round` only on a revision, `Cox-Merged-With` only on a
    collision's commit."""
    trailers = [
        f"Cox-Initiative: {initiative}", f"Cox-Phase: {phase}", f"Cox-Task: {task}", f"Cox-Run: {run_id}",
        f"Cox-Kind: {kind}",
        *([f"Cox-Round: {round_}"] if kind == "revision" else []),
        *([f"Cox-Merged-With: {merged_with}"] if merged_with else []),
    ]
    title_line = " ".join(title.split()) or task
    return f"epic {run_id}: {task}\n\n{title_line}\n\n" + "\n".join(trailers) + "\n"


def branch_name(initiative: str, phase: str) -> str:
    """The phase branch on origin: `epic/<initiative>/<phase>`, as the old model and the lander name it."""
    return f"epic/{initiative}/{phase}"


def _git(*args: str, cwd: Path, stdin: str | None = None) -> tuple[bool, str, str]:
    """Edge. One git command that never waits on a prompt: (ok, stdout, stderr)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", *args], cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                              timeout=_TIMEOUT_S, stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "", f"git {' '.join(args[:2])} timed out after {_TIMEOUT_S}s"
    return done.returncode == 0, done.stdout, done.stderr.strip()


def _private(run_id: str, name: str) -> str:
    return f"refs/coxswain/lanes/{_SAFE.sub('_', run_id)}/{name}"


def fetch(repo: Path, run_id: str, branch: str) -> tuple[str | None, str]:
    """Edge. Origin's tip of `branch` as (sha, ""), (None, "") when origin has no such branch, or (None, why) when git
    could not say. Fetched to a ref private to the run."""
    ok, out, err = _git("ls-remote", "--heads", "origin", f"refs/heads/{branch}", cwd=repo)
    if not ok:
        return None, f"git ls-remote origin {branch} failed: {err[:300]}"
    if not out.strip():
        return None, ""
    ref = _private(run_id, branch)
    ok, _, err = _git("fetch", "--no-tags", "origin", f"+refs/heads/{branch}:{ref}", cwd=repo)
    if not ok:
        return None, f"git fetch origin {branch} failed: {err[:300]}"
    ok, sha, err = _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=repo)
    return (sha.strip(), "") if ok else (None, f"{ref} does not resolve: {err[:300]}")


def log_since(repo: Path, base: str, tip: str) -> tuple[bool, str]:
    """Edge. `git log` of `base..tip` in `lane_progress.LOG_ARGS` form, for `lane_progress.read`."""
    ok, out, err = _git("log", *LOG_ARGS, f"{base}..{tip}", cwd=repo)
    return ok, out if ok else err


def open_phase_worktree(repo: Path, path: Path, sha: str) -> tuple[bool, str]:
    """Edge. A worktree of `repo` at `path`, detached at `sha`, where this lane's roll-ups are committed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with registry_lock(repo):
        ok, _, err = _git("worktree", "add", "--detach", str(path), sha, cwd=repo)
    return ok, err


def change(task_worktree: Path, base: str) -> tuple[bool, str]:
    """Edge. The task's whole change against the tip it was cut from, binary-safe, for `apply_and_commit`."""
    ok, out, err = _git("diff", "--binary", "--no-renames", base, "HEAD", cwd=task_worktree)
    return ok, out if ok else err


def head(worktree: Path) -> str:
    ok, out, _ = _git("rev-parse", "HEAD", cwd=worktree)
    return out.strip() if ok else ""


def reset_to(worktree: Path, sha: str) -> None:
    """Edge. Put the phase worktree back at `sha`, index and files: after a change that did not apply, or a push that
    was refused, nothing of it stays behind for the next roll-up."""
    _git("reset", "--hard", "-q", sha, cwd=worktree)


def apply_and_commit(phase_worktree: Path, diff: str, text: str) -> tuple[str | None, str]:
    """Edge. Apply `diff` cleanly onto the phase worktree's HEAD and commit it with message `text`: (the commit, "")
    or (None, why). An empty change still commits, so the task's trailer, which is its progress, is on the branch."""
    before = head(phase_worktree)
    if diff.strip():
        ok, _, err = _git("apply", "--index", "--whitespace=nowarn", "-", cwd=phase_worktree, stdin=diff)
        if not ok:
            reset_to(phase_worktree, before)
            return None, f"the change does not apply cleanly onto the phase tip {before[:12]}: {err[:500]}"
    ok, _, err = _git(*IDENTITY, "commit", "-q", "--allow-empty", "--no-verify", "-F", "-", cwd=phase_worktree,
                      stdin=text)
    if not ok:
        reset_to(phase_worktree, before)
        return None, f"the roll-up could not be committed: {err[:500]}"
    return head(phase_worktree), ""


def push(phase_worktree: Path, branch: str) -> tuple[bool, str]:
    """Edge. Fast-forward origin's `branch` to the phase worktree's HEAD; never forced, so a branch another lane moved
    meanwhile refuses it."""
    ok, _, err = _git("push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}", cwd=phase_worktree)
    return ok, err


@contextmanager
def phase_lock(repo: Path, initiative: str, phase: str) -> Iterator[None]:
    """Edge. The phase's local lock, an exclusive `flock` on a file in the repository's git directory: held by one
    roll-up at a time across every lane and thread on this machine. Each holder opens the file itself, so two threads
    of one process exclude each other too."""
    ok, out, err = _git("rev-parse", "--git-common-dir", cwd=repo)
    if not ok:
        raise RuntimeError(f"no git directory for {repo}: {err[:200]}")
    path = (repo / out.strip()).resolve() / f"coxswain-lane-{_SAFE.sub('_', initiative)}-{_SAFE.sub('_', phase)}.lock"
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
