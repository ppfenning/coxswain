"""Pure class and effort step rules for the model router.

Ladders are ordered tuples, lowest rung first. Nothing here reads the clock, the
environment or randomness, and nothing imports the catalog or bounds: clipping to
what a team may spend is a later step. Each rule returns a value and a reason.

Call counts: `History.role_calls[role]` is the number of calls the role has made,
counting the current one. The challenger fires when that count is a positive
multiple of `every`, so 10 fires and 9 and 11 do not. The source select_tier in
coxswain-tools was not readable when this was ported; this reading is the one to
check against it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

NEVER_BELOW_FLOOR = frozenset({"build", "arbitrate"})
DEGRADED = "go_degraded"


@dataclass(frozen=True)
class History:
    role_calls: Mapping[str, int]
    landed_rate: float = 1.0


@dataclass(frozen=True)
class Hints:
    revise_or_retry: bool = False
    diff_lines: int = 0
    file_count: int = 0
    pacing_state: str = "go"


def _step(ladder: tuple[str, ...], current: str, delta: int) -> str:
    """Move `delta` rungs along the ladder, stopping at either end."""
    return ladder[max(0, min(len(ladder) - 1, ladder.index(current) + delta))]


def floor_class(role: str, ladder: tuple[str, ...], floors: Mapping[str, str]) -> tuple[str, str]:
    """Rule 1. A role with no floor, or a floor off the ladder, starts at the lowest rung."""
    floor = floors.get(role)
    if floor in ladder:
        return floor, f"floor {floor} for {role}"
    return ladder[0], f"no usable floor for {role}, lowest rung {ladder[0]}"


def retry_bump(hints: Hints) -> tuple[int, str]:
    """Rule 2."""
    if hints.revise_or_retry:
        return 1, "+1 revise or retry"
    return 0, "not a revise or retry"


def size_bump(hints: Hints, diff_threshold: int, file_threshold: int) -> tuple[int, str]:
    """Rule 3. Strictly past either threshold."""
    if hints.diff_lines > diff_threshold or hints.file_count > file_threshold:
        return 1, f"+1 size: {hints.diff_lines} diff lines, {hints.file_count} files"
    return 0, "within size thresholds"


def degraded_drop(hints: Hints) -> tuple[int, str]:
    """Rule 4."""
    if hints.pacing_state == DEGRADED:
        return -1, "-1 pacing go_degraded"
    return 0, "pacing not degraded"


def challenger_drop(role: str, history: History, reviewed: frozenset[str], every: int = 10) -> tuple[int, str]:
    """Rule 5. One step down on every Nth call of a reviewed seat."""
    calls = history.role_calls.get(role, 0)
    if role in reviewed and calls > 0 and calls % every == 0:
        return -1, f"-1 challenger on call {calls}"
    return 0, "no challenger"


def escalation_step(
    role: str, history: History, hold_below_calls: int = 20, landed_floor: float = 0.70
) -> tuple[int, str]:
    """Ported from select_tier: hold under `hold_below_calls`, then +1 while landed rate is under the floor."""
    calls = history.role_calls.get(role, 0)
    if calls < hold_below_calls:
        return 0, f"hold, {calls} calls under {hold_below_calls}"
    if history.landed_rate < landed_floor:
        return 1, f"+1 landed rate {history.landed_rate} under {landed_floor}"
    return 0, f"landed rate {history.landed_rate} holds"


def _exempt(role: str, never_below_floor: frozenset[str], step: tuple[int, str]) -> tuple[int, str]:
    """Rule 6. Rules 4 and 5 are no-ops for a role that never goes below floor."""
    if role in never_below_floor:
        return 0, f"{role} never goes below floor"
    return step


def _settle(ladder: tuple[str, ...], start: str, steps: tuple[tuple[int, str], ...]) -> tuple[str, str]:
    fired = tuple(reason for delta, reason in steps if delta != 0)
    return _step(ladder, start, sum(delta for delta, _ in steps)), "; ".join(fired) or "no rule fired"


def select_class(
    role: str,
    ladder: tuple[str, ...],
    floors: Mapping[str, str],
    hints: Hints,
    history: History,
    *,
    reviewed: frozenset[str],
    diff_threshold: int,
    file_threshold: int,
    never_below_floor: frozenset[str] = NEVER_BELOW_FLOOR,
    challenge_every: int = 10,
    hold_below_calls: int = 20,
    landed_floor: float = 0.70,
) -> tuple[str, str]:
    floor, floor_reason = floor_class(role, ladder, floors)
    steps = (
        retry_bump(hints),
        size_bump(hints, diff_threshold, file_threshold),
        _exempt(role, never_below_floor, degraded_drop(hints)),
        _exempt(role, never_below_floor, challenger_drop(role, history, reviewed, challenge_every)),
        escalation_step(role, history, hold_below_calls, landed_floor),
    )
    chosen, reason = _settle(ladder, floor, steps)
    return chosen, f"{floor_reason}; {reason}"


def select_effort(
    role: str,
    effort_ladder: tuple[str, ...],
    base_effort: str,
    hints: Hints,
    history: History,
    *,
    reviewed: frozenset[str],
    diff_threshold: int,
    file_threshold: int,
    never_below_floor: frozenset[str] = NEVER_BELOW_FLOOR,
    challenge_every: int = 10,
) -> tuple[str, str]:
    steps = (
        retry_bump(hints),
        size_bump(hints, diff_threshold, file_threshold),
        _exempt(role, never_below_floor, degraded_drop(hints)),
        _exempt(role, never_below_floor, challenger_drop(role, history, reviewed, challenge_every)),
    )
    chosen, reason = _settle(effort_ladder, base_effort, steps)
    return chosen, f"base {base_effort}; {reason}"
