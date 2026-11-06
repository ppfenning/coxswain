"""Readers for the epic steward's facts: reported events from the bus, the task signature and level merged state."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools import courier
from agent_tools.chair_steward import StewardTask, event_key
from agent_tools.chair_steward_level import GitRun, LevelState, level_state


def reported_keys(entries: Sequence[Mapping], topic: str | None = None) -> set[str]:
    """The `event_key` of each typed entry, so a once-only rule holds across a chair restart.

    Typed means a str `event` and a `fields` dict of str values; anything else is ignored.
    `topic` keeps only that topic's entries, because a key does not carry its topic.
    """
    return {
        event_key(e["event"], dict(e["fields"]))
        for e in entries
        if isinstance(e.get("event"), str)
        and isinstance(e.get("fields"), dict)
        and all(isinstance(v, str) for v in e["fields"].values())
        and (topic is None or e.get("topic") == topic)
    }


def task_signature(tasks: Sequence[StewardTask]) -> str:
    """Stable signature of task states: equal for the same (id, state) pairs in any order."""
    return ",".join(f"{t['id']}={t['state']}" for t in sorted(tasks, key=lambda t: t["id"]))


def level_branch(initiative: str, phase: str) -> str:
    """The harness names a level's branch `epic/<initiative>/<phase>`."""
    return f"epic/{initiative}/{phase}"


def read_bus_entries(path: Path) -> list[dict]:
    """Every bus entry at its latest state; a missing or unreadable file is an empty bus."""
    try:
        return courier.entries(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def level_is_merged(repo: str, ticket_branch: str, level: str, run: GitRun) -> bool:
    """True only when the level has landed on the ticket branch; a conflict or a git error is not merged."""
    try:
        return level_state(repo, ticket_branch, level, run) is LevelState.MERGED
    except RuntimeError:
        return False
