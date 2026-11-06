"""The orchestration rule: which hosts are active, what mode results, and when the toggle refuses.

Pure. The caller passes `now` as an ISO string. The chair loop, the toggle and the status line all read this
module, so they cannot disagree. A hosts-table row has `name`, `state` and `beat_at`; the states are
`active`, `draining` and `removed`.
"""
from collections.abc import Mapping, Sequence
from typing import Any

from agent_tools.chair_login_watch import PLACEMENT_STALE_S
from agent_tools.chair_read_lost import stale_hosts

Row = Mapping[str, Any]

FALLBACK_NOTICE = "orchestration mode on but no other active host; building on the chair machine"
REFUSAL = "orchestration needs another active lane host"


def active_hosts(rows: Sequence[Row], now: str) -> list[str]:
    """Names of active rows with a fresh beat, in table order. A row that never beat counts as active, as placement does."""
    return [
        str(row.get("name") or "")
        for row in rows
        if row.get("state") == "active"
        and (not row.get("beat_at") or not stale_hosts([row], now, threshold_s=PLACEMENT_STALE_S))
    ]


def orchestration_available(rows: Sequence[Row], now: str) -> bool:
    return len(active_hosts(rows, now)) >= 2


def effective_local_lanes(configured: str, lane_host_names: Sequence[str]) -> str:
    """`decompose` only when configured so and a lane host exists; every other value reads as `any`."""
    return "decompose" if configured == "decompose" and lane_host_names else "any"


def mode_name(configured: str, lane_host_names: Sequence[str]) -> str:
    return "orchestrating" if effective_local_lanes(configured, lane_host_names) == "decompose" else "building"


def toggle_refusal(target: str, rows: Sequence[Row], now: str) -> str | None:
    """None for `off`, and for `on` when orchestration is available. Any other target is refused like `on` without a host."""
    if target == "off" or (target == "on" and orchestration_available(rows, now)):
        return None
    return REFUSAL


def mode_line(configured: str, rows: Sequence[Row], local: str, now: str) -> str:
    """`<local>: <mode> (available|unavailable)`; the lane hosts are the active hosts other than `local`."""
    lane_hosts = [name for name in active_hosts(rows, now) if name != local]
    available = "available" if orchestration_available(rows, now) else "unavailable"
    return f"{local}: {mode_name(configured, lane_hosts)} ({available})"
