"""`cox chair status`: who runs the chair's work now. The chair lease, each queue kind's worker lease, and the units
that keep them running (C6). The cutover's check (§5.4 step 5) reads it.

Pure: the rows come in, the lines go out. Every age is on the store's clock, as the leases are; the edge
(`cli_chair_units`) reads the rows, the store's clock and the units' states.

There is no `chair_queue` table (C3 to C5 re-derive each pass from the store's rows), so a queue has no depth and no
oldest item to show; the status says so rather than print a zero that would read as an empty queue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_tools.chair_kinds import LEASE_PREFIX, QUEUE_KINDS

__all__ = ["LeaseRow", "Standing", "all_live", "kind_standing", "lease_line", "status_lines"]

_RELEASED = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class LeaseRow:
    """One `leases` row as read: every value as the store gave it."""

    name: str
    holder: str
    epoch: int
    heartbeat_at: str
    expires_at: str


@dataclass(frozen=True)
class Standing:
    """A lease's state at the store's `now`: live, expired, released or absent, with its beat's and expiry's ages."""

    state: str
    holder: str = ""
    epoch: int = 0
    beat_age_s: float | None = None
    expires_in_s: float | None = None


def _at(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.strip().replace(" ", "T")) if raw else None
    except ValueError:
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def kind_standing(row: LeaseRow | None, now: datetime) -> Standing:
    """Pure. The lease's standing at `now`. A row with no holder, or one expiring at the Unix epoch, is released."""
    if row is None:
        return Standing("absent")
    expires, beat = _at(row.expires_at), _at(row.heartbeat_at)
    beat_age = (now - beat).total_seconds() if beat is not None else None
    if not row.holder or expires == _RELEASED:
        return Standing("released", row.holder, row.epoch, beat_age)
    left = (expires - now).total_seconds() if expires is not None else None
    state = "live" if left is not None and left > 0 else "expired"
    return Standing(state, row.holder, row.epoch, beat_age, left)


def _span(seconds: float) -> str:
    whole = int(abs(seconds))
    if whole < 120:
        return f"{whole}s"
    if whole < 7200:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{(whole % 3600) // 60:02d}m"


def lease_line(name: str, standing: Standing) -> str:
    """Pure. One lease as a line: its name, its state, its holder and epoch, and how old its last beat is."""
    if standing.state == "absent":
        return f"{name}: no lease row"
    beat = f", beat {_span(standing.beat_age_s)} ago" if standing.beat_age_s is not None else ""
    if standing.state == "live" and standing.expires_in_s is not None:
        tail = f", expires in {_span(standing.expires_in_s)}"
    elif standing.state == "expired" and standing.expires_in_s is not None:
        tail = f", expired {_span(standing.expires_in_s)} ago"
    else:
        tail = ""
    holder = standing.holder or "nobody"
    return f"{name}: {standing.state}, held by {holder} at epoch {standing.epoch}{beat}{tail}"


def all_live(queued: frozenset[str], kinds: Mapping[str, Standing]) -> bool:
    """Pure. Whether every listed kind has a live worker: what `cox chair status` exits 0 on."""
    return all(kinds.get(kind, Standing("absent")).state == "live" for kind in queued)


def status_lines(
    queued: frozenset[str], chair: LeaseRow | None, rows: Sequence[LeaseRow], now: datetime,
    units: Mapping[str, str] | None = None,
) -> list[str]:
    """Pure. The status: the setting, the chair lease, one line per kind, the units' states when known, and the queues.

    `rows` are the `chair:<kind>` lease rows; `units` maps a unit (or launchd label) to its state, None when the
    platform has none to read."""
    by_kind = {row.name.removeprefix(LEASE_PREFIX): row for row in rows if row.name.startswith(LEASE_PREFIX)}
    listed = ", ".join(k for k in QUEUE_KINDS if k in queued) or "none (the tick plans every kind)"
    lines = [
        f"chair.queues: {listed}",
        f"store clock: {now.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",
        lease_line("chair (the tick's lease)", kind_standing(chair, now)),
        "workers:",
    ]
    for kind in QUEUE_KINDS:
        standing = kind_standing(by_kind.get(kind), now)
        mark = "listed" if kind in queued else "not listed"
        lines.append(f"  {lease_line(f'{LEASE_PREFIX}{kind} ({mark})', standing)}")
    if units is not None:
        lines.append("units:")
        lines.extend(f"  {name}: {state or 'unknown'}" for name, state in units.items())
    lines.append("queues: no depth or oldest age to show; each pass re-derives its kind's work from the store's rows")
    standings = {kind: kind_standing(by_kind.get(kind), now) for kind in QUEUE_KINDS}
    dead = [kind for kind in QUEUE_KINDS if kind in queued and standings[kind].state != "live"]
    if dead:
        lines.append(f"NOT RUNNING: {', '.join(dead)} (listed in chair.queues, no live worker)")
    return lines
