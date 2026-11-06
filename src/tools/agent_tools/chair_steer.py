"""The pure overlap rule every launch path shares; the edge fills `running` and `steer_streaks`."""
from collections.abc import Mapping, Sequence

from agent_tools.chair_types import Action, RunningInitiative

ESCALATE_AT = 3  # consecutive prior deferrals of one ordered pair before the chair is asked
_NEW_MARKER = " (new)"


def _clean(entry: str) -> str:
    return entry.removesuffix(_NEW_MARKER)


def _ignored(path: str) -> bool:
    """A path under a `snapshots` directory, or a lock file, never counts as shared."""
    parts = path.split("/")
    base = parts[-1]
    return "snapshots" in parts or base.endswith(".lock") or base == "package-lock.json"


def _overlap(x: str, y: str) -> str | None:
    if x == y:
        return x
    if x.endswith("/") and y.startswith(x):
        return y
    if y.endswith("/") and x.startswith(y):
        return x
    return None


def shared_paths(a: Sequence[str], b: Sequence[str]) -> list[str]:
    """Equal entries, or a `/`-ending directory and anything under it; reports the longer, sorted and unique."""
    left = [p for p in map(_clean, a) if not _ignored(p)]
    right = [p for p in map(_clean, b) if not _ignored(p)]
    found = {hit for x in left for y in right if (hit := _overlap(x, y)) is not None}
    return sorted(found)


def _first_overlap(surfaces: Sequence[str], entries: Sequence[RunningInitiative]) -> tuple[RunningInitiative, list[str]] | None:
    return next(((e, paths) for e in entries if (paths := shared_paths(surfaces, e["surfaces"]))), None)


def steer_check(
    initiative: str,
    repo: str,
    surfaces: Sequence[str],
    running: Sequence[RunningInitiative],
    streaks: Mapping[str, int],
    phase: str | None = None,
) -> Action | None:
    """The first same-repo overlap with another initiative: steer_clear, or needs_chair once the streak reaches 3.

    Only with no such overlap, a sibling phase of the same initiative that shares a path gives steer_clear, never needs_chair.
    An entry or candidate without a phase is the whole initiative and never a sibling.
    """
    same_repo = [e for e in running if e["repo"] == repo]
    cross = _first_overlap(surfaces, [e for e in same_repo if e["id"] != initiative])
    if cross is not None:
        entry, paths = cross
        other = entry["id"]
        if streaks.get(f"{initiative}|{other}", 0) < ESCALATE_AT:
            other_phase = entry.get("phase")
            named: Action = {} if phase is None else {"phase": phase, **({} if other_phase is None else {"other_phase": other_phase})}
            return {"kind": "steer_clear", "initiative": initiative, "other": other, **named, "paths": paths}
        return {
            "kind": "needs_chair",
            "initiative": initiative,
            "cause": "steer_deferred",
            "reason": f"{initiative} shares {', '.join(paths)} with running {other}; the pair has been deferred three ticks",
        }
    if phase is None:
        return None
    siblings = [e for e in same_repo if e["id"] == initiative and e.get("phase") not in (None, phase)]
    sibling = _first_overlap(surfaces, siblings)
    if sibling is None:
        return None
    entry, paths = sibling
    return {"kind": "steer_clear", "initiative": initiative, "other": initiative, "phase": phase, "other_phase": entry["phase"], "paths": paths}
