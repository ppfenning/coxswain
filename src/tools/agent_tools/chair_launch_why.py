"""Pure naming of why each ready initiative was not launched this tick."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import NotRequired, TypedDict

from agent_tools.chair_launch_budget import WINDOW
from agent_tools.chair_types import Action, ActionKind, InitiativeFacts, LaunchRecord
from core.jsontypes import is_mapping


class Placement(TypedDict, total=False):
    """What the schedule says of an initiative's placement: the lane host of its first launch, or why it waits."""

    host: str  # "" is the chair machine
    scheduled_later: str


class UnlaunchedInitiative(InitiativeFacts, Placement):
    """An initiative's facts with the optional keys the caller supplies (see the note above `unlaunched`)."""

    backoff_until: NotRequired[str]
    hold: NotRequired[str]
    target_release: NotRequired[str]
    current_version: NotRequired[str]


LEFT_OUT = "left out of the launch list for a reason the facts do not show"
_STEER_KINDS = frozenset({"needs_chair", "steer_clear"})
_BUDGET = "launch budget"
_BUDGET_REFUSED = "relaunch refused by the budget or the loop rule"
_CAP_SPENT = "launch cap reached this tick"
_MAX = re.compile(r"max_launches_per_hour=(\d+)")


def _parse_at(entry: object) -> datetime | None:
    """An aware time from an ISO string or an {"at": ISO} record; a naive or malformed entry reads as None."""
    raw = entry.get("at") if is_mapping(entry) else entry
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _clears_at(text: str | None, now: datetime, history: Sequence[str | LaunchRecord]) -> str | None:
    """When the hourly budget named by `text` clears: the exit that drops the window count below its max.

    None unless `text` is the budget's own reason with a max, so a loop-rule or capacity refusal gets no time."""
    found = _MAX.search(text or "")
    if not text or not text.startswith(_BUDGET) or found is None:
        return None
    inside = sorted(t for t in map(_parse_at, history) if t is not None and now - WINDOW < t <= now)
    over = len(inside) - int(found.group(1))
    return f"clears at {(inside[over] + WINDOW).isoformat()}" if over >= 0 else None


def _withheld(initiative: UnlaunchedInitiative, withheld_by: Mapping[str, Collection[str]], note: str | None) -> str | None:
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


def _steer_held(id: str, steer_holds: Sequence[Action]) -> str | None:
    held = next((a for a in steer_holds if a.get("kind") in _STEER_KINDS and a.get("initiative") == id and a.get("other")), None)
    return None if held is None else f"steer hold: waiting on {held.get('other')}"


def _version(text: object) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in str(text).split("."))
    except ValueError:
        return None


def _release_held(initiative: UnlaunchedInitiative) -> str | None:
    """A hold the facts carry, or a target release above the current version."""
    if hold := initiative.get("hold"):
        return f"on hold: {hold}"
    target_text, current_text = initiative.get("target_release"), initiative.get("current_version")
    target, current = _version(target_text), _version(current_text)
    above = target is not None and current is not None and target > current
    return f"target release {target_text} is above current version {current_text}" if above else None


# Optional initiative keys the caller supplies: host, scheduled_later, dag_problem, backoff_until, hold,
# target_release, current_version.
# A missing key claims nothing. host == "" is the one fact that says it would build on the chair machine: the caller
# sets host from the real placement (the name of the lane host the schedule or fill chose, "" for the chair machine)
# and leaves it absent when the initiative was not placed. scheduled_later is returned verbatim; dag_problem is
# reported as an invalid DAG ahead of any withheld name. A steer action with no "other" is skipped.
# The budget expiry needs the skip text for the initiative; a withheld refusal with no such skip may be the loop rule.
# cap_spent is a caller fact: a cap above 0 was used up by earlier launches this tick. It ignores the host field.
def unlaunched(
    initiatives: Sequence[UnlaunchedInitiative],
    launched: Collection[str],
    withheld_by: Mapping[str, Collection[str]],
    skips: Mapping[str, str],
    steer_holds: Sequence[Action],
    free_lanes: int,
    no_host: bool,
    orchestrating: bool,
    pace_cap: int,
    now: datetime,
    launch_history: Sequence[str | LaunchRecord],
    *,
    cap_spent: bool = False,
) -> list[Action]:
    """One unlaunched action per ready initiative not in launched, naming the first reason that applies."""
    def reason(i: UnlaunchedInitiative) -> str:
        local = i.get("host") == ""
        note = _clears_at(skips.get(i["id"]), now, launch_history)
        candidates = (
            f"invalid DAG: {problem}" if (problem := i.get("dag_problem")) else None,
            _withheld(i, withheld_by, note),
            _skipped(i["id"], skips, note),
            _steer_held(i["id"], steer_holds),
            i.get("scheduled_later") or None,
            "paced against the weekly target" if pace_cap == 0 else None,
            "orchestration mode: it would build on the chair machine" if orchestrating and local else None,
            _CAP_SPENT if cap_spent else None,
            _release_held(i),
            "no lane free on an eligible host" if no_host or (free_lanes <= 0 and local) else None,
        )
        return next((c for c in candidates if c), LEFT_OUT)

    return [
        {"kind": ActionKind.UNLAUNCHED, "initiative": i["id"], "reason": reason(i)}
        for i in initiatives
        if i["ready_tasks"] and i["id"] not in launched
    ]
