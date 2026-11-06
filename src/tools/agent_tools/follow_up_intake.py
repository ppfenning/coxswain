"""File one follow-up for another repository as a queued intake that waits on the current initiative.

A pure core builds the intake file with `route.intake_file`; a thin edge hands it to `route.write_filed_item`."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from agent_tools import route

__all__ = ["file_follow_up", "follow_up_intake_file"]


def follow_up_intake_file(follow_up: Mapping, *, after: str, date: str, home: str) -> dict:
    """Pure. `route.intake_file`'s mapping for `follow_up`, waiting on `after`; the body opens with its lint sentence."""
    body = f"lint: {follow_up['lint']}\n\n{follow_up['body']}"
    repo = route.repo_field(follow_up["repo"], home)
    return route.intake_file(follow_up["title"], body, repo, date, after=[after])


def file_follow_up(
    runs_dir: Path, workspace: Path, follow_up: Mapping, *, after: str, date: str, home: str
) -> str | None:
    """Edge. Write the intake under `workspace` and its queue row; its relative path, or None after a printed line.

    The path comes from the title and date, so filing the same follow-up again rewrites the one file."""
    ((rel, text),) = follow_up_intake_file(follow_up, after=after, date=date, home=home).items()
    path = Path(workspace) / rel
    if route.write_filed_item(runs_dir, path, "intake", (path.name,), text) != 0:
        print(f"routing: follow-up {follow_up['title']!r} was not filed at {path}")
        return None
    return rel
