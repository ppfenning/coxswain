"""Reader: did main's CI go green at or after a commit.

Takes the same injected runner as chair_revert_ports. Nothing here spawns a process.
"""

from __future__ import annotations

import json
import os

from agent_tools.chair_revert_ports import Runner
from core.jsontypes import is_dict, is_list

__all__ = ["main_green_since"]

_RUN_LIMIT = "50"


def _successful_shas(output: str) -> list[str]:
    """headSha of each completed, successful run in `gh run list` JSON output; [] when it is not a JSON list."""
    try:
        runs = json.loads(output)
    except ValueError:
        return []
    if not is_list(runs):
        return []
    return [
        sha
        for run in runs
        if is_dict(run)
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and isinstance(sha := run.get("headSha"), str)
    ]


Ancestry = dict[tuple[str, str, str], bool]  # (repo, commit, sha) to whether commit is an ancestor of sha
GIT_FATAL = 128  # git's exit for an object it cannot find, or a path that is no repository


def _ancestry(runner: Runner, repo: str, commit: str, sha: str, known: Ancestry | None) -> int:
    """The exit of `git merge-base --is-ancestor commit sha`: 0 yes, 1 no, anything else a failure. An answer is kept in
    `known`, since a commit's ancestry never changes; a failure is not, since a commit not fetched yet may be next time."""
    key = (repo, commit, sha)
    if known is not None and key in known:
        return 0 if known[key] else 1
    code = runner(["git", "-C", repo, "merge-base", "--is-ancestor", commit, sha])[0]
    if known is not None and code in (0, 1):
        known[key] = code == 0
    return code


def main_green_since(runner: Runner, repo: str, slug: str, commit: str, known: Ancestry | None = None) -> bool:
    """True when a successful main run sits on `commit` or on a descendant of it. Any failure reads as False.
    `repo` is a land watch's checkout path, often in the tilde form: no shell expands it for `git -C`, so it is
    expanded here, or every ancestor check fails and a main that went green after a red land never reads green.
    `known` is the caller's ancestry answers, kept across calls: a quiet tick asks git nothing it asked before."""
    repo = os.path.expanduser(repo)
    try:
        code, output = runner(
            [
                "gh", "run", "list", "--repo", slug, "--branch", "main",
                "--json", "headSha,status,conclusion", "--limit", _RUN_LIMIT,
            ]
        )  # fmt: skip
        if code != 0:
            return False
        shas = _successful_shas(output)
        if commit in shas or not shas:
            return commit in shas
        # A commit git cannot find, or a path that is no repository, fails every check against it: one check of the
        # commit against itself stands for the fifty that would each fail the same way.
        if _ancestry(runner, repo, commit, commit, known) == GIT_FATAL:
            return False
        return any(_ancestry(runner, repo, commit, sha, known) == 0 for sha in shas)
    except Exception:
        return False
