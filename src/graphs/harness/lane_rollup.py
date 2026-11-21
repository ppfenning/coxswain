"""The phase lane's git edge: where the phase branch stands on origin, one task's roll-up onto it, and the clean-up of
what a run leaves in the repository.

A task rolls up as exactly one commit on the phase branch, linear, never a merge: its change (the task worktree's tree
against the tip it was cut from) is applied cleanly onto the phase tip and committed with the `Cox-*` trailers
(`message`, plan §1.4), then pushed. The push is a plain fast-forward, never forced. A change that does not apply
cleanly is reported as such (`did_not_apply`): resolving it is the merge agent's (`lane_merge`, L5), not this module's.

**Every call that reaches origin is bounded** to `REMOTE_TIMEOUT_S`, well inside the lane's claim margin
(`phase_pick.LANE_MARGIN_S`), with git's own transport timeouts set beneath it and the command's whole process group
killed when it runs over: a lane that checked its claim and then pushes cannot still be pushing once the margin it
checked for has run out. A push ends in one of three outcomes (`push_outcome`): `pushed`; `rejected`, when origin's
branch moved (another lane landed a commit); `error`, anything else (the network, a timeout), which says nothing about
the claim. What the roll-up does next is `next_move`'s, pure.

The phase worktree is detached at the tip and pushed as `HEAD:refs/heads/<branch>`, so no local branch is created,
moved or checked out. That is why `phase_remote.push_branch` and `fetch_branch` are not used: both work on a local
branch of the same name (and the fetch moves it), and neither is bounded to the margin. Origin's refs are fetched to
refs private to the run (`refs/coxswain/lanes/<run>/...`), dropped when the run ends (`drop_refs`); a run killed before
it could drop them, with its worktrees and scratch branches, is swept by the next lane of the phase (`sweep`).
`phase_lock` is the phase's local lock: every lane on this machine that rolls up into one phase takes it, each thread
of a lane too, from the apply to the push.

`message`, `branch_name`, `push_outcome`, `next_move` and `stale_runs` are pure; everything else is an edge.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

from harness.epic_branches import IDENTITY
from harness.lane_progress import LOG_ARGS
from harness.worktree import registry_lock

__all__ = [
    "REMOTE_TIMEOUT_S", "apply_and_commit", "branch_name", "change", "did_not_apply", "drop_refs", "fetch", "head",
    "log_since",
    "message", "next_move", "open_phase_worktree", "phase_lock", "private_ref", "push", "push_outcome", "reset_to",
    "stale_runs", "sweep", "sweep_runs",
]

_TIMEOUT_S = 120  # a local git command
_REAP_S = 2  # collecting a command killed for running over its bound
_NOT_APPLIED = "the change does not apply cleanly"
REMOTE_TIMEOUT_S = 8  # a call that reaches origin: well inside phase_pick.LANE_MARGIN_S (15 s)
# git's own transport bounds beneath REMOTE_TIMEOUT_S: an ssh that cannot connect or stops answering, an http
# transfer that stalls.
_REMOTE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=4 -o ServerAliveInterval=2 -o ServerAliveCountMax=2",
}
_REMOTE_CONFIG = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=4")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_LANES = "refs/coxswain/lanes"
# What git prints when a push is refused because the branch moved, rather than failing to arrive.
_REJECTED = ("[rejected]", "non-fast-forward", "fetch first", "stale info")

PushOutcome = Literal["pushed", "rejected", "error"]
Move = Literal["done", "adopt", "lost", "reapply", "retry", "fail"]


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


def push_outcome(returncode: int | None, output: str) -> PushOutcome:
    """Pure. What a push came to from its exit (None when it was killed for running over its bound) and what it
    printed: `pushed`, `rejected` when origin refused it because the branch moved, else `error`."""
    if returncode == 0:
        return "pushed"
    if returncode is not None and any(marker in output for marker in _REJECTED):
        return "rejected"
    return "error"


def next_move(held: bool, origin_tip: str | None, ours: str, parent: str, landed: bool, attempt: int,
              attempts: int = 3) -> Move:
    """Pure. What a roll-up does after a push that did not come back `pushed`, from the claim re-checked, origin's tip
    of the branch as fetched now (None when it could not be read), the commit it tried to push and the tip it was
    made on, whether origin's branch already holds a commit of this task (a stale lane landed it), and which attempt
    this was.

    - The claim is gone: `lost`, and the lane stops.
    - Origin holds our commit (the push arrived though its answer did not): `done`.
    - Origin holds the task's commit from another lane: `adopt` it as this task's roll-up.
    - Origin's branch moved on: `reapply` the change onto the new tip and push again.
    - Origin did not move (a network error, a timeout): `retry` the same push.
    Past `attempts` tries, a reapply or retry is `fail`: that task fails alone, the claim and the lane stand."""
    if not held:
        return "lost"
    if origin_tip == ours:
        return "done"
    if landed:
        return "adopt"
    if attempt >= attempts:
        return "fail"
    if origin_tip is not None and origin_tip != parent:
        return "reapply"
    return "retry"


def did_not_apply(why: str) -> bool:
    """Pure. Whether `apply_and_commit`'s reason says the change did not apply onto the tip (a collision, the merge
    agent's), rather than that the commit failed."""
    return why.startswith(_NOT_APPLIED)


def stale_runs(names: Iterable[str], prefix: str, own: str) -> list[str]:
    """Pure. Of the run ids `names` (as they appear in ref and directory names), those of the phase (`<prefix>-<n>`)
    other than `own`, sorted: the runs a new lane of the phase sweeps up after."""
    pattern = re.compile(rf"{re.escape(_SAFE.sub('_', prefix))}-\d+")
    return sorted({n for n in names if pattern.fullmatch(n) and n != _SAFE.sub("_", own)})


def _git(*args: str, cwd: Path, stdin: str | None = None) -> tuple[bool, str, str]:
    """Edge. One local git command that never waits on a prompt: (ok, stdout, stderr)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", *args], cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                              timeout=_TIMEOUT_S, stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "", f"git {' '.join(args[:2])} timed out after {_TIMEOUT_S}s"
    return done.returncode == 0, done.stdout, done.stderr.strip()


def _remote(*args: str, cwd: Path, timeout_s: float = REMOTE_TIMEOUT_S) -> tuple[int | None, str, str]:
    """Edge. One git command that reaches origin, bounded to `timeout_s`, in a session of its own so its whole group
    (ssh, a remote helper) is killed when it runs over: (exit, or None when killed, stdout, stderr)."""
    proc = subprocess.Popen(["git", *_REMOTE_CONFIG, *args], cwd=cwd, env={**os.environ, **_REMOTE_ENV},
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        _reap(proc)
        return None, "", f"git {args[0]} timed out after {timeout_s}s and was killed"
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    return proc.returncode, out, err.strip()


def _reap(proc: subprocess.Popen[str]) -> None:
    """Edge. Collect a killed command, bounded to `_REAP_S`: a grandchild that left the group can hold its pipes open
    past the kill, and the lane must not wait on it. Its pipes are closed then; the process is reaped if it can be."""
    try:
        proc.communicate(timeout=_REAP_S)
    except subprocess.TimeoutExpired:
        for pipe in (proc.stdout, proc.stderr):
            with suppress(OSError):
                if pipe is not None:
                    pipe.close()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_REAP_S)


def _private(run_id: str, name: str) -> str:
    return f"{_LANES}/{_SAFE.sub('_', run_id)}/{name}"


def fetch(repo: Path, run_id: str, branch: str) -> tuple[str | None, str]:
    """Edge. Origin's tip of `branch` as (sha, ""), (None, "") when origin has no such branch, or (None, why) when git
    could not say. Fetched to a ref private to the run; each call bounded to `REMOTE_TIMEOUT_S`."""
    code, out, err = _remote("ls-remote", "--heads", "origin", f"refs/heads/{branch}", cwd=repo)
    if code != 0:
        return None, f"git ls-remote origin {branch} failed: {err[:300]}"
    if not out.strip():
        return None, ""
    ref = _private(run_id, branch)
    code, _, err = _remote("fetch", "--no-tags", "origin", f"+refs/heads/{branch}:{ref}", cwd=repo)
    if code != 0:
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
    """Edge. The worktree's HEAD commit, "" when git cannot say."""
    ok, out, _ = _git("rev-parse", "HEAD", cwd=worktree)
    return out.strip() if ok else ""


def reset_to(worktree: Path, sha: str) -> None:
    """Edge. Put the phase worktree at `sha`, index and files: after a change that did not apply or a push that did
    not land, nothing of it stays behind; after origin moved, the worktree follows it."""
    _git("reset", "--hard", "-q", sha, cwd=worktree)


def apply_and_commit(phase_worktree: Path, diff: str, text: str) -> tuple[str | None, str]:
    """Edge. Apply `diff` cleanly onto the phase worktree's HEAD and commit it with message `text`: (the commit, "")
    or (None, why). An empty change still commits, so the task's trailer, which is its progress, is on the branch."""
    before = head(phase_worktree)
    if diff.strip():
        ok, _, err = _git("apply", "--index", "--whitespace=nowarn", "-", cwd=phase_worktree, stdin=diff)
        if not ok:
            reset_to(phase_worktree, before)
            return None, f"{_NOT_APPLIED} onto the phase tip {before[:12]}: {err[:500]}"
    ok, _, err = _git(*IDENTITY, "commit", "-q", "--allow-empty", "--no-verify", "-F", "-", cwd=phase_worktree,
                      stdin=text)
    if not ok:
        reset_to(phase_worktree, before)
        return None, f"the roll-up could not be committed: {err[:500]}"
    return head(phase_worktree), ""


def push(phase_worktree: Path, branch: str) -> tuple[PushOutcome, str]:
    """Edge. Fast-forward origin's `branch` to the phase worktree's HEAD, never forced, bounded to `REMOTE_TIMEOUT_S`:
    its outcome (`push_outcome`) and what git said."""
    code, out, err = _remote("push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}", cwd=phase_worktree)
    said = f"{out}\n{err}".strip()
    return push_outcome(code, said), said


def drop_refs(repo: Path, run_id: str) -> None:
    """Edge. Delete every ref the run fetched origin into (`refs/coxswain/lanes/<run>/...`)."""
    ok, out, _ = _git("for-each-ref", "--format=%(refname)", f"{_LANES}/{_SAFE.sub('_', run_id)}/", cwd=repo)
    for ref in out.split() if ok else []:
        _git("update-ref", "-d", ref, cwd=repo)


def sweep(repo: Path, worktree_root: Path, prefix: str, own: str) -> list[str]:
    """Edge. What earlier runs of the phase left on this machine, which a run killed before its own clean-up leaves:
    their private refs, their worktrees under `worktree_root/<run>` and their scratch branches (`agents/<run>/...`).
    Only a lane of the phase calls it, once its worker has stopped every other lane of the phase here, so none of
    those runs is live. The runs swept."""
    ok, refs, _ = _git("for-each-ref", "--format=%(refname)", f"{_LANES}/", "refs/heads/agents/", cwd=repo)
    names = {r.split("/")[3] for r in refs.split() if r.count("/") >= 4} if ok else set[str]()
    dirs = {p.name for p in worktree_root.iterdir() if p.is_dir()} if worktree_root.is_dir() else set[str]()
    runs = stale_runs(names | dirs, prefix, own)
    sweep_runs(repo, worktree_root, runs)
    return runs


def sweep_runs(repo: Path, worktree_root: Path, runs: Sequence[str]) -> None:
    """Edge. Remove what each of `runs`, none of them live, left: its private refs and scratch branches in `repo`, and
    its folder under `worktree_root` (its worktrees, and its own clone when it had one)."""
    for run in runs:
        drop_refs(repo, run)
        ok, branches, _ = _git("for-each-ref", "--format=%(refname:short)", f"refs/heads/agents/{run}/", cwd=repo)
        folder = worktree_root / run
        if folder.is_dir():
            with registry_lock(repo):
                shutil.rmtree(folder, ignore_errors=True)
                _git("worktree", "prune", cwd=repo)
        for branch in branches.split() if ok else []:
            _git("branch", "-D", branch, cwd=repo)


def private_ref(run_id: str, name: str) -> str:
    """Pure. A ref private to the run (`refs/coxswain/lanes/<run>/<name>`), which `drop_refs` and `sweep` remove."""
    return _private(run_id, name)


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
