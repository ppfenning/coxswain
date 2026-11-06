from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

READY = "ready"
RED = "red"
MISSING = "missing"
PENDING = "pending"
UNKNOWN = "unknown"
NONE_REQUIRED = "none_required"


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str


def _latest(rows: Sequence[Mapping[str, str]]) -> dict[str, str]:
    return {row["name"]: row["state"] for row in rows}


def verdict(required: tuple[str, ...] | None, rows: Sequence[Mapping[str, str]]) -> Verdict:
    """Classify required checks; the newest-listed row wins per name, and failure beats missing beats pending."""
    if required is None:
        return Verdict(UNKNOWN, "required checks could not be read")
    if not required:
        return Verdict(NONE_REQUIRED, "no checks are required")
    latest = _latest(rows)
    failed = [n for n in required if latest.get(n) == "failure"]
    absent = [n for n in required if n not in latest]
    # Pending and any state that is neither success nor failure (skipped) both block readiness.
    waiting = [n for n in required if n in latest and latest[n] not in ("success", "failure")]
    if failed:
        return Verdict(RED, "failed: " + ", ".join(failed))
    if absent:
        return Verdict(MISSING, "not reported: " + ", ".join(absent))
    if waiting:
        return Verdict(PENDING, "not green yet: " + ", ".join(waiting))
    return Verdict(READY, "all required checks succeeded")
