"""Pure warm pool rules: fingerprint, sync decision, sizing, eviction."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# Not valid hex, so it cannot collide with a real sha256 digest.
MISSING_LOCK_DIGEST = "no-lockfile"


@dataclass(frozen=True)
class PoolEntry:
    path: str
    slot: str
    synced_digest: str | None
    last_used: float
    leased: bool


def lock_digest(content: bytes | None) -> str:
    if content is None:
        return MISSING_LOCK_DIGEST
    return hashlib.sha256(content).hexdigest()


def sync_decision(synced_digest: str | None, current_digest: str) -> Literal["warm", "resync"]:
    if synced_digest is not None and synced_digest == current_digest:
        return "warm"
    return "resync"


def pool_size(lane_capacity: int, spare: int = 1) -> int:
    """Worktrees kept per repository: lane capacity plus spare, at least 1."""
    return max(1, lane_capacity + spare)


def choose_evictions(entries: Sequence[PoolEntry], keep: int) -> list[str]:
    """Slot names to evict, oldest first; leased entries are never eligible. Negative keep counts as 0."""
    idle = [e for e in entries if not e.leased]
    excess = len(idle) - max(0, keep)
    if excess <= 0:
        return []
    ordered = sorted(idle, key=lambda e: (e.last_used, e.slot))
    return [e.slot for e in ordered[:excess]]


def setup_record(mode: str, seconds: float) -> dict[str, object]:
    """Timing record for 'warm' or 'cold'; any other mode returns {'error': ...} instead of raising."""
    if mode not in ("warm", "cold"):
        return {"error": f"unknown setup mode: {mode!r}"}
    return {"mode": mode, "seconds": round(seconds, 3)}
