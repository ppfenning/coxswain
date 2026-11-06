"""Pure decision helpers for checking that a build base holds the merged work a task needs. No I/O, git, clock or store."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Expected:
    task: str
    commit: str
    why: str


def _counts(item: Mapping[str, Any]) -> bool:
    """A task counts when it has a stored merge commit and is done, or approved with merged true."""
    state = item.get("state")
    return bool(item.get("merged_commit")) and (state == "done" or (state == "approved" and bool(item.get("merged"))))


def expected_merges(
    items: Sequence[Mapping[str, Any]],
    phase: str,
    task_ids: Collection[str],
    parents: Collection[str],
) -> tuple[Expected, ...]:
    """Merged tasks the base must hold, sorted by task id. A task both needed and in a parent phase is `need`.

    `phase` is the building task's own phase; no rule uses it yet.
    """
    by_id = {str(i.get("id")): i for i in items}
    needed = {n for t in task_ids for n in (by_id.get(t) or {}).get("needs") or ()}
    wanted = {
        **{str(i.get("id")): "parent" for i in items if i.get("phase") in parents},
        **dict.fromkeys(needed, "need"),
    }
    return tuple(
        Expected(task=task, commit=str(by_id[task]["merged_commit"]), why=why)
        for task, why in sorted(wanted.items())
        if task in by_id and _counts(by_id[task])
    )


def gaps(expected: Iterable[Expected], present: Mapping[str, bool]) -> tuple[Expected, ...]:
    """The expected entries whose task is False or missing in `present`, in the given order."""
    return tuple(e for e in expected if present.get(e.task) is not True)


def stale_base_stop(gaps: Sequence[Expected]) -> str:
    """The stop sentence naming every missing task, sorted; empty when nothing is missing."""
    if not gaps:
        return ""
    names = ", ".join(sorted({g.task for g in gaps}))
    return f"{gaps[0].task}: the build base lacks the merged work of {names} and could not be rebuilt"
