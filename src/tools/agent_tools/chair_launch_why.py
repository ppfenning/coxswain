"""Pure naming of why each ready initiative was not launched this tick."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any

from agent_tools.chair_launch_budget import WINDOW

LEFT_OUT = "left out of the launch list for a reason the facts do not show"
_STEER_KINDS = frozenset({"needs_chair", "steer_clear"})
_BUDGET = "launch budget"
_BUDGET_REFUSED = "relaunch refused by the budget or the loop rule"
_MAX = re.compile(r"max_launches_per_hour=(\d+)")


def _parse_at(entry: Any) -> datetime | None:
    """An aware time from an ISO string or an {"at": ISO} record; a naive or malformed entry reads as None."""
    raw = entry.get("at") if isinstance(entry, Mapping) else entry
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def _clears_at(text: str | None, now: datetime, history: Sequence[Any]) -> str | None:
    """When the hourly budget named by `text` clears: the exit that drops the window count below its max.

    None unless `text` is the budget's own reason with a max, so a loop-rule or capacity refusal gets no time."""
    found = _MAX.search(text or "")
    if not text or not text.startswith(_BUDGET) or found is None:
        return None
    inside = sorted(t for t in map(_parse_at, history) if t is not None and now - WINDOW < t <= now)
    over = len(inside) - int(found.group(1))
    return f"clears at {(inside[over] + WINDOW).isoformat()}" if over >= 0 else None


def _withheld(initiative: Mapping[str, Any], withheld_by: Mapping[str, Collection[str]], note: str | None) -> str | None:
    name = next((n for n, ids in withheld_by.items() if initiative["id"] in ids), None)
    backoff_end = initiative.get("backoff_until")
    extra = f"ends at {backoff_end}" if name == "land backoff" and backoff_end else note if name == _BUDGET_REFUSED else None
    return name if name is None or extra is None else f"{name}, {extra}"


def _skipped(id: str, skips: Mapping[str, str], note: str | None) -> str | None:
    text = skips.get(id)
    if text is None:
        return None
    base = text if text.startswith(_BUDGET) else f"{_BUDGET}: {text}"
    return f"{base}, {note}" if note and text.startswith(_BUDGET) else base


def _steer_held(id: str, steer_holds: Sequence[Mapping[str, Any]]) -> str | None:
    held = next((a for a in steer_holds if a.get("kind") in _STEER_KINDS and a.get("initiative") == id and a.get("other")), None)
    return None if held is None else f"steer hold: waiting on {held['other']}"


def _version(text: Any) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in str(text).split("."))
    except ValueError:
        return None


def _release_held(initiative: Mapping[str, Any]) -> str | None:
    """A hold the facts carry, or a target release above the current version."""
    if initiative.get("hold"):
        return f"on hold: {initiative['hold']}"
    target, current = _version(initiative.get("target_release")), _version(initiative.get("current_version"))
    above = target is not None and current is not None and target > current
    return f"target release {initiative['target_release']} is above current version {initiative['current_version']}" if above else None


# Optional initiative keys the caller supplies: host, scheduled_later, dag_problem, backoff_until, hold,
# target_release, current_version.
# A missing key claims nothing. host == "" is the one fact that says it would build on the chair machine: the caller
# sets host from the real placement (the name of the lane host the schedule or fill chose, "" for the chair machine)
# and leaves it absent when the initiative was not placed. scheduled_later is returned verbatim; dag_problem is
# reported as an invalid DAG ahead of any withheld name. A steer action with no "other" is skipped.
# The budget expiry needs the skip text for the initiative; a withheld refusal with no such skip may be the loop rule.
def unlaunched(
    initiatives: Sequence[Mapping[str, Any]],
    launched: Collection[str],
    withheld_by: Mapping[str, Collection[str]],
    skips: Mapping[str, str],
    steer_holds: Sequence[Mapping[str, Any]],
    free_lanes: int,
    no_host: bool,
    orchestrating: bool,
    pace_cap: int,
    now: datetime,
    launch_history: Sequence[Any],
) -> list[dict[str, str]]:
    """One unlaunched action per ready initiative not in launched, naming the first reason that applies."""
    def reason(i: Mapping[str, Any]) -> str:
        local = i.get("host") == ""
        note = _clears_at(skips.get(i["id"]), now, launch_history)
        candidates = (
            f"invalid DAG: {i['dag_problem']}" if i.get("dag_problem") else None,
            _withheld(i, withheld_by, note),
            _skipped(i["id"], skips, note),
            _steer_held(i["id"], steer_holds),
            i.get("scheduled_later") or None,
            "orchestration mode: it would build on the chair machine" if orchestrating and local else None,
            "paced against the weekly target" if pace_cap == 0 else None,
            _release_held(i),
            "no lane free on an eligible host" if no_host or (free_lanes <= 0 and local) else None,
        )
        return next((c for c in candidates if c), LEFT_OUT)

    return [
        {"kind": "unlaunched", "initiative": i["id"], "reason": reason(i)}
        for i in initiatives
        if i["ready_tasks"] and i["id"] not in launched
    ]
