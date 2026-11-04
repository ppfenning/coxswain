"""Read a repository's file listing. The only place this feature touches disk or a process."""

import functools
import os
import subprocess
from pathlib import Path

_ORIGIN_HEAD_PREFIX = "refs/remotes/origin/"
_FETCH_TIMEOUT_SECONDS = 60


def _default_branch(symref: str | None) -> str:
    """Branch named by the text of `refs/remotes/origin/HEAD`; `main` when absent or unrecognised."""
    ref = (symref or "").strip()
    branch = ref.removeprefix(_ORIGIN_HEAD_PREFIX) if ref.startswith(_ORIGIN_HEAD_PREFIX) else ""
    return branch or "main"


def _git(repo_path: Path, *args: str, timeout: float | None = None) -> str | None:
    """Stdout of `git <args>` in `repo_path`, or None on a non-zero exit or a process failure.

    Discovery stops at `repo_path`, so a plain directory inside some other checkout is not read as that checkout.
    """
    env = {
        **os.environ,
        "GIT_CEILING_DIRECTORIES": str(repo_path.resolve().parent),
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        done = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return done.stdout.decode("utf-8", "surrogateescape") if done.returncode == 0 else None


@functools.cache  # process-lifetime memo at the edge: a tick linting many initiatives fetches a repo once
def _fetch_origin(resolved_path: Path) -> bool:
    """Fetch origin; False when it failed, which callers treat as "use the refs already local"."""
    return _git(resolved_path, "fetch", "--quiet", "origin", timeout=_FETCH_TIMEOUT_SECONDS) is not None


def _git_listing(repo_path: Path) -> str | None:
    """NUL-separated paths on origin's default branch, or None when the ref cannot be read."""
    _fetch_origin(repo_path.resolve())
    branch = _default_branch(_git(repo_path, "symbolic-ref", "refs/remotes/origin/HEAD"))
    return _git(repo_path, "ls-tree", "-r", "-z", "--name-only", f"origin/{branch}")


def _walk_listing(repo_path: Path) -> frozenset[str]:
    """Repo-relative forward-slash paths of the working tree, .git skipped."""
    found = []
    for root, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        found.extend(Path(root, name).relative_to(repo_path).as_posix() for name in filenames)
    return frozenset(found)


def _choose_listing(git_out: str | None, walked: frozenset[str]) -> frozenset[str]:
    """The origin default-branch listing when the ref was read, else the walked working tree."""
    if git_out is not None:
        return frozenset(p for p in git_out.split("\0") if p)
    return walked


def read_checkout(repo_path: str | Path) -> frozenset[str]:
    """Repo-relative paths in the checkout; empty means the directory is missing."""
    path = Path(repo_path)
    if not path.is_dir():
        return frozenset()
    git_out = _git_listing(path)
    walked = frozenset() if git_out is not None else _walk_listing(path)
    return _choose_listing(git_out, walked)
