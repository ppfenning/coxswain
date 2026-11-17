"""Pure no-progress guard (docs/hardening.md H4.1): hold an initiative whose last runs all ended the same way.

An initiative whose last N ended runs (`chair.no_progress_after`, default 2) ended with the same phase states and no
new task state is held for a person, with a `needs you:` reason that names the repeated outcome. Relaunching it again
would only end the same way: something outside the run (an approval, a merge, a fix) has to change first.

A run is compared by its phase states (`phases` rows of the run store) and the task states it recorded (its
`task_records` rows); two runs that built nothing record no task, so their task states are equal. A run with no phase
record says nothing about its outcome, so it never counts toward a repeat. Only runs after the initiative's
fresh-start mark count (`core.workstore.FRESH_AFTER_RUN`, written by `cox route release`), so a release after a fix
gets N fresh runs before the guard can hold it again. No I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from core.workstore import counts_after

DEFAULT_NO_PROGRESS_AFTER = 2  # `chair.no_progress_after` when the profile does not set it; 0 turns the guard off
CAUSE = "no_progress"
_REASON_CHARS = 300  # a phase reason is quoted up to this many characters


class RunOutcome(TypedDict):
    """One ended run of an initiative: its phase states and reasons, and the state of each task it recorded."""

    run_id: str
    phases: dict[str, str]
    reasons: dict[str, str]
    tasks: dict[str, str]


class NoProgress(TypedDict):
    """The repeated runs, newest first, and the hold reason that names their outcome."""

    runs: list[str]
    reason: str


def resolve_no_progress_after(value: object) -> int:
    """The profile's raw `chair.no_progress_after`: 0 (never hold) or a whole number of at least 2, else the default.
    One run cannot repeat itself, so 1 reads as the default too, as do a bool, a fraction, a negative number and text."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        return DEFAULT_NO_PROGRESS_AFTER
    return int(value) if value == 0 or value >= 2 else DEFAULT_NO_PROGRESS_AFTER


def _signature(outcome: RunOutcome) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    return tuple(sorted(outcome["phases"].items())), tuple(sorted(outcome["tasks"].items()))


def repeated(outcomes: Sequence[RunOutcome], after: int, cutoff: Mapping[str, int]) -> list[RunOutcome] | None:
    """The newest `after` counted runs, newest first, when they all ended with the same phase states and task states;
    else None. `outcomes` arrive newest first; a run at or before `cutoff` (its series' fresh-start mark) is not counted.
    None too when `after` is 0, when fewer runs count, or when one of them recorded no phase."""
    if after < 2:
        return None
    counted = [o for o in outcomes if counts_after(o["run_id"], cutoff)][:after]
    if len(counted) < after or any(not o["phases"] for o in counted):
        return None
    first = _signature(counted[0])
    return counted if all(_signature(o) == first for o in counted) else None


def outcome_text(outcome: RunOutcome) -> str:
    """`p1 partial, p2 blocked`, then the first phase that did not complete and its own reason, when it gave one."""
    states = sorted(outcome["phases"].items())
    text = ", ".join(f"{phase} {state}" for phase, state in states)
    stuck = next((p for p, s in states if s != "complete" and outcome["reasons"].get(p)), None)
    if stuck is None:
        return text
    reason = " ".join(outcome["reasons"][stuck].split())
    return f"{text}; {stuck}: {reason[:_REASON_CHARS]}{'...' if len(reason) > _REASON_CHARS else ''}"


def hold_reason(initiative: str, runs: Sequence[RunOutcome]) -> str:
    """The `needs you:` reason: the runs oldest first, the outcome they repeated, and the release that resets the count."""
    ids = ", ".join(o["run_id"] for o in reversed(runs))
    return (
        f"needs you: no progress: runs {ids} ended the same way and changed no task: {outcome_text(runs[0])}. "
        f"Fix the cause, then `cox route release {initiative}`"
    )


def no_progress_facts(
    outcomes: Mapping[str, Sequence[RunOutcome]], cutoffs: Mapping[str, Mapping[str, int]], after: int
) -> dict[str, NoProgress]:
    """Per initiative whose newest counted runs repeated one outcome: the runs, newest first, and the hold reason."""
    found = {i: runs for i, own in outcomes.items() if (runs := repeated(own, after, cutoffs.get(i, {}))) is not None}
    return {i: {"runs": [o["run_id"] for o in runs], "reason": hold_reason(i, runs)} for i, runs in sorted(found.items())}


def no_progress_key(initiative: str, newest_run: str) -> str:
    """The handled key of one hold: a later repeat, after a release, ends on a newer run and so is held again."""
    return f"{CAUSE}:{initiative}:{newest_run}"


def plan_no_progress(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One hold_initiative per initiative in `facts["no_progress"]` that is not held already and whose key is not handled."""
    held = frozenset(facts.get("held_initiatives", []))
    handled = frozenset(facts.get("handled_keys", []))
    return [
        {"kind": "hold_initiative", "initiative": initiative, "cause": CAUSE, "reason": found["reason"], "key": key}
        for initiative, found in sorted((facts.get("no_progress") or {}).items())
        if initiative not in held and (key := no_progress_key(initiative, found["runs"][0])) not in handled
    ]
