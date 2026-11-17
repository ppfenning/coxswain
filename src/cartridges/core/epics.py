from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import TypedDict

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def valid_epic_id(value: object) -> str:
    """A lowercase slug: letters, digits and hyphens, starting with a letter or digit."""
    if isinstance(value, str) and _SLUG.fullmatch(value):
        return value
    raise ValueError(f"epic id must be a lowercase slug, got {value!r}")


class EpicInitiative(TypedDict):
    id: str
    landed: bool


class EpicRecord(TypedDict):
    id: str
    title: object
    target_branch: object
    initiatives: list[EpicInitiative]
    landed: int
    total: int
    complete: bool


def _record(epic: str, ids: list[str], landed: Collection[str], definition: Mapping[str, object]) -> EpicRecord:
    initiatives = [EpicInitiative(id=i, landed=i in landed) for i in sorted(ids)]
    done = sum(1 for i in initiatives if i["landed"])
    return EpicRecord(
        id=epic,
        title=definition.get("title") or epic,
        target_branch=definition.get("target_branch"),
        initiatives=initiatives,
        landed=done,
        total=len(initiatives),
        complete=bool(initiatives) and done == len(initiatives),
    )


def rollup(
    members: Mapping[str, str | None],
    landed: Collection[str],
    definitions: Mapping[str, Mapping[str, object]],
) -> list[EpicRecord]:
    """One record per epic that has members, sorted by epic id; initiatives without an epic are left out."""
    grouped: dict[str, list[str]] = {}
    for initiative, epic in members.items():
        if epic is not None:
            grouped.setdefault(epic, []).append(initiative)
    return [_record(epic, grouped[epic], landed, definitions.get(epic) or {}) for epic in sorted(grouped)]


def format_epic_line(record: Mapping[str, object]) -> str:
    line = f"{record['id']} {record['landed']}/{record['total']} landed"
    return f"{line} complete" if record["complete"] else line
