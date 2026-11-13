"""The backlog manifest's migration step: mark the tasks one pull request carried as done.

`prereq-backlog-manifest.json` lists approved tasks whose commits were folded into a single pull request by hand,
outside the harness. Once that pull request has merged, each task it lists as applied is on the parent, so it is
`done`. This step says so in the work store. It does nothing until the forge says the pull request merged, it moves
only a task that is `approved`, and a task already done is skipped, so it is safe to run twice.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APPLIED = "applied"


@dataclass(frozen=True)
class Step:
    initiative: str
    task: str
    action: str  # done, skip
    why: str = ""


def read_manifest(path: Path) -> list[dict[str, Any]]:
    """The manifest's task rows; a missing or unreadable file raises ValueError with the path."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"backlog manifest {path} could not be read: {exc}") from exc
    tasks = data.get("tasks") if isinstance(data, Mapping) else None
    if not isinstance(tasks, list):
        raise ValueError(f"backlog manifest {path} has no tasks list")
    return [dict(t) for t in tasks if isinstance(t, Mapping)]


def plan(manifest: Sequence[Mapping[str, Any]], state_of: Callable[[str, str], str | None]) -> list[Step]:
    """Pure but for `state_of(initiative, task)`. One step per manifest task, in manifest order."""
    steps = []
    for row in manifest:
        initiative, task, status = str(row.get("initiative") or ""), str(row.get("task_id") or ""), str(row.get("status") or "")
        if not status.startswith(APPLIED):
            steps.append(Step(initiative, task, "skip", f"the manifest says: {status or 'no status'}"))
            continue
        state = state_of(initiative, task)
        if state == "approved":
            steps.append(Step(initiative, task, "done"))
        else:
            steps.append(Step(initiative, task, "skip", "already done" if state == "done" else f"its state is {state or 'unknown'}, not approved"))
    return steps


def apply(steps: Sequence[Step], mark_done: Callable[[str, str, str], str | None], pr: str) -> list[str]:
    lines = []
    for step in steps:
        if step.action != "done":
            lines.append(f"{step.task}: skipped: {step.why}")
            continue
        why = mark_done(step.initiative, step.task, pr)
        lines.append(f"{step.task}: done" if why is None else f"{step.task}: not marked done: {why}")
    return lines


def lines(steps: Sequence[Step]) -> list[str]:
    return [f"{s.task}: would be marked done" if s.action == "done" else f"{s.task}: skipped: {s.why}" for s in steps]
