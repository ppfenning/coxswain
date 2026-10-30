"""A repo's integration branch: the one place coxswain decides what `main` means for a repo.

Order: the `--base` flag, the profile's `repo_base` entry for the repo, the repo's `origin/HEAD`, then `main`.
Every base used for a cut, a merge or a PR is `origin/<branch>` after a `git fetch origin`; a local branch is never read.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping


class BaseMissing(ValueError):
    """The repo has an origin but no `origin/<base>`: a mistyped `--base` or a stale `repo_base` entry."""


def resolve_base(flag: str | None, repo_base: str | None, origin_head: str | None) -> str:
    """The first of `flag`, `repo_base`, `origin_head` that is non-empty, else `main`."""
    return flag or repo_base or origin_head or "main"


def repo_base(profile: Mapping, repo: str) -> str | None:
    """The profile's `repo_base` entry for `repo`, found by its path (`~` expanded) or its directory name."""
    table = profile.get("repo_base") or {}
    if not isinstance(table, Mapping):
        return None
    path = os.path.expanduser(repo)
    for key in (repo, path, os.path.basename(path.rstrip("/"))):
        if table.get(key):
            return str(table[key])
    return None


def _git(repo: str, *args: str) -> tuple[int, str]:
    done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout.strip()


def branch_name(repo: str, flag: str | None = None, base: str | None = None) -> str:
    """Edge. The bare integration branch name for `repo`, `origin/HEAD` read from its checkout."""
    code, head = _git(repo, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    return resolve_base(flag, base, head.removeprefix("origin/") if code == 0 and head else None)


def base_ref(repo: str, flag: str | None = None, base: str | None = None) -> str:
    """Edge. `origin/<branch>` for `repo`; call it after `git fetch origin`.

    A repo with no `origin` remote at all (a bare test fixture) gets the bare local name. A repo with an origin
    but no `origin/<branch>` raises BaseMissing; it never falls back to a local branch."""
    branch = branch_name(repo, flag, base)
    code, remotes = _git(repo, "remote")
    if code == 0 and "origin" not in remotes.split():
        return branch
    if code != 0:  # git cannot read the repo here, so there is nothing to verify against
        return f"origin/{branch}"
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")[0] != 0:
        raise BaseMissing(f"runs land: origin/{branch} not found in {repo}")
    return f"origin/{branch}"
