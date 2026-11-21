"""One lock for the writers of the chair's workspace checkout: the pull, the fill, the export and the commit.

After the cutover these run in more than one process at once: housekeeping's pull, fill, export and commit, and the
launch worker's epic launch, which fills and exports. Without a lock a fill reads files a pull is rewriting, an export
deletes files a fill has not read, and two `git` calls race for `.git/index.lock`. Each writer takes an exclusive
`flock` on `coxswain-workspace.lock` in the workspace's git dir (the workspace itself when it is not in git).

The wait is bounded (`BOUND_SECONDS`). Past it the writer is skipped: `held` logs one line naming the holder and
raises `WorkspaceBusy`, and each caller turns that into the skip it can afford (the pull's reason line, a refused
launch, an export retried on the next pass). The lock is reentrant within a process, so the export's own fill and the
commit that follows it never wait on the lock their caller already holds; `flock` alone would, since a second open of
the file is a second lock."""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

LOCK_NAME = "coxswain-workspace.lock"
BOUND_SECONDS = 120.0
POLL_SECONDS = 0.05
SKIP_PREFIX = "workspace busy:"


class WorkspaceBusy(RuntimeError):
    """The lock stayed held past the bound; the writer was skipped."""


def git_dir(workspace: Path) -> Path:
    """The workspace's git dir, read without running git: `.git` itself, the dir a `.git` file names (a worktree or a
    submodule), else the workspace, for a workspace out of git."""
    dot_git = workspace / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        text = dot_git.read_text(encoding="utf-8")
    except OSError:
        return workspace
    named = text.strip().removeprefix("gitdir:").strip() if text.startswith("gitdir:") else ""
    if not named:
        return workspace
    path = Path(named)
    return path if path.is_absolute() else (workspace / path).resolve()


def lock_path(workspace: Path) -> Path:
    return git_dir(workspace) / LOCK_NAME


def skip_line(what: str, bound: float, holder: str) -> str:
    """Pure. The one line logged for a skipped writer."""
    by = f" by {holder}" if holder else ""
    return f"{SKIP_PREFIX} skipped the {what}; the workspace lock was held{by} past {bound:g}s"


def skipped(note: str) -> bool:
    """Pure. Whether a note is a skip line, so a caller can retry on its next pass rather than on the next move."""
    return SKIP_PREFIX in note


# Edge state: the process's own hold, so a nested writer (an export's fill) does not wait on its caller's flock.
_mutex = threading.RLock()
_depth = [0]
_fd: list[int | None] = [None]


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _holder(fd: int) -> str:
    with contextlib.suppress(OSError):
        return os.pread(fd, 200, 0).decode("utf-8", "replace").strip()
    return ""


@contextlib.contextmanager
def held(
    workspace: Path, what: str, bound: float | None = None, log: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Edge. Hold the workspace lock for `what` (`pull`, `fill`, `export`, `commit`), waiting at most `bound` seconds;
    past it, log the skip and raise `WorkspaceBusy` without running the body."""
    say = log or (lambda line: print(line, file=sys.stderr))
    bound = BOUND_SECONDS if bound is None else bound  # read at the call, so one setting bounds every writer
    deadline = clock() + bound
    while not _mutex.acquire(blocking=False):  # another thread of this process holds it
        if clock() >= deadline:
            line = skip_line(what, bound, f"another thread of pid {os.getpid()}")
            say(line)
            raise WorkspaceBusy(line)
        sleep(POLL_SECONDS)
    try:
        path = lock_path(workspace)
        if _depth[0] == 0 and path.parent.is_dir():  # no workspace at all: nothing to write, nothing to race
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                while not _try_flock(fd):
                    if clock() >= deadline:
                        line = skip_line(what, bound, _holder(fd))
                        say(line)
                        raise WorkspaceBusy(line)
                    sleep(POLL_SECONDS)
            except BaseException:
                os.close(fd)
                raise
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
                os.pwrite(fd, f"pid {os.getpid()} ({what})\n".encode(), 0)
            _fd[0] = fd
        _depth[0] += 1
        try:
            yield
        finally:
            _depth[0] -= 1
            if _depth[0] == 0 and _fd[0] is not None:
                fd, _fd[0] = _fd[0], None
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)
                os.close(fd)  # closing the only descriptor of this open releases the flock
    finally:
        _mutex.release()
