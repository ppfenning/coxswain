"""Decide whether a level branch has landed on its ticket branch, by ancestry or by content."""

from collections.abc import Callable
from enum import StrEnum

GitRun = Callable[[list[str]], tuple[int, str]]


class LevelState(StrEnum):
    MERGED = "merged"
    NOT_MERGED = "not_merged"
    CONFLICT = "conflict"


def level_merged(is_ancestor: bool, trial_tree: str | None, ticket_tree: str) -> LevelState:
    """Ancestry wins; a trial tree equal to the ticket tree means a squash already landed the content.

    trial_tree is None when the trial merge failed or conflicted.
    """
    if is_ancestor:
        return LevelState.MERGED
    if trial_tree is None:
        return LevelState.CONFLICT
    return LevelState.MERGED if trial_tree == ticket_tree else LevelState.NOT_MERGED


def _git(repo: str, *args: str) -> list[str]:
    return ["-C", repo, *args]


def level_state(repo: str, ticket_branch: str, level_branch: str, run: GitRun) -> LevelState:
    """Classify the level against the ticket branch. `run` takes git arguments, without the leading `git`.

    `repo` must already be an expanded absolute path.
    Reads refs and trees and asks `git merge-tree --write-tree`, which writes only objects.
    It never touches the index, the working tree, or any ref. Needs git 2.38 or later.
    """
    ancestor_code, ancestor_out = run(_git(repo, "merge-base", "--is-ancestor", level_branch, ticket_branch))
    if ancestor_code not in (0, 1):
        raise RuntimeError(f"git merge-base --is-ancestor exited {ancestor_code}: {ancestor_out.strip()}")
    tree_code, tree_out = run(_git(repo, "rev-parse", f"{ticket_branch}^{{tree}}"))
    if tree_code != 0:
        raise RuntimeError(f"git rev-parse {ticket_branch}^{{tree}} exited {tree_code}: {tree_out.strip()}")
    trial_code, trial_out = run(_git(repo, "merge-tree", "--write-tree", ticket_branch, level_branch))
    trial_tree = trial_out.splitlines()[0].strip() if trial_code == 0 and trial_out.strip() else None
    return level_merged(ancestor_code == 0, trial_tree, tree_out.strip())
