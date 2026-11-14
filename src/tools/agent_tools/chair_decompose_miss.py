"""Pure decompose-miss backoff: an intake whose idea file is missing backs off, then escalates. No file, process or clock access."""

from dataclasses import dataclass
from datetime import datetime, timedelta

MISS_ESCALATE_AT = 3
MISS_BACKOFF_BASE_TICKS = 4
MISS_BACKOFF_CAP_SECONDS = 7200


@dataclass(frozen=True)
class DecomposeMiss:
    intake_id: str
    idea_path: str  # the path the launch tried
    count: int  # consecutive misses
    last_miss_at: str  # ISO UTC
    fingerprint: str  # changes when the intake's content, status or idea path changes


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def next_miss(
    prev: DecomposeMiss | None,
    intake_id: str,
    idea_path: str,
    fingerprint: str,
    now_iso: str,
) -> DecomposeMiss:
    count = prev.count + 1 if prev is not None and prev.fingerprint == fingerprint else 1
    return DecomposeMiss(
        intake_id=intake_id,
        idea_path=idea_path,
        count=count,
        last_miss_at=now_iso,
        fingerprint=fingerprint,
    )


def backoff_seconds(count: int, tick_seconds: int) -> int:
    ticks = MISS_BACKOFF_BASE_TICKS * 2 ** (max(count, 1) - 1)
    return min(ticks * tick_seconds, MISS_BACKOFF_CAP_SECONDS)


def is_skipped(miss: DecomposeMiss, current_fingerprint: str, now_iso: str, tick_seconds: int) -> bool:
    if current_fingerprint != miss.fingerprint:
        return False
    if miss.count >= MISS_ESCALATE_AT:
        return True
    until = _parse(miss.last_miss_at) + timedelta(seconds=backoff_seconds(miss.count, tick_seconds))
    return _parse(now_iso) < until


def needs_escalation(miss: DecomposeMiss, reported: bool) -> bool:
    return miss.count >= MISS_ESCALATE_AT and not reported


def escalation_reason(miss: DecomposeMiss) -> str:
    return (
        f"Intake {miss.intake_id} missed its idea file {miss.idea_path} "
        f"{miss.count} times in a row; decompose is skipped until the intake changes."
    )
