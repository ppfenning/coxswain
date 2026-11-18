"""The landing record and the land queue in the store: every read and write of the two tables of migration 0016.

Each function takes an open `Connection`. Times are UTC strings (`%Y-%m-%dT%H:%M:%SZ`), which compare in order as
text. A queue item moves `queued -> claimed -> done | failed`, and back to `queued` when it is enqueued again or
its failed attempt is to be retried; a `claimed` item whose `lease_until` has passed may be claimed again.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.landing_record import LandingRecord, Listing, Unreadable, listing, read_row
from harness.store_dialect import Connection, json_text, upsert

PHASE_MERGE = "phase_merge"
INITIATIVE_LAND = "initiative_land"
ITEM_KINDS = (PHASE_MERGE, INITIATIVE_LAND)
ITEM_STATES = ("queued", "claimed", "done", "failed")

_RECORD_COLUMNS = ("initiative", "repo", "state", "record_json", "created_at", "updated_at")
_ITEM_COLUMNS = (
    "id", "kind", "initiative", "phase", "repo", "parent", "state", "reason", "queued_at", "claimed_by", "claimed_at",
    "lease_until", "attempts", "last_error", "updated_at",
)


def item_id(kind: str, initiative: str, phase: str = "") -> str:
    """One item per phase merge, one per initiative land: the key that makes enqueueing idempotent."""
    if kind not in ITEM_KINDS:
        raise ValueError(f"land queue kind must be one of {', '.join(ITEM_KINDS)}, got {kind!r}")
    return f"{kind}:{initiative}:{phase}" if kind == PHASE_MERGE else f"{kind}:{initiative}"


# -- landing records ----------------------------------------------------------------------------------------------

class UnreadableRecord(ValueError):
    """`get_record` of a row that does not read as a landing record. Only that initiative's reader stops."""

    def __init__(self, found: Unreadable) -> None:
        super().__init__(f"the landing record of {found.initiative} cannot be read: {found.why}")
        self.unreadable = found


def read_record(conn: Connection, initiative: str) -> LandingRecord | Unreadable | None:
    """The initiative's record, `Unreadable` when its row does not read as one, None when it has no row."""
    p = conn.dialect.placeholder
    row = conn.query_one(f"SELECT record_json FROM landing_records WHERE initiative = {p}", (initiative,))
    return None if row is None else read_row(initiative, row[0])


def get_record(conn: Connection, initiative: str) -> LandingRecord | None:
    """The initiative's record, None when it has none. `UnreadableRecord` when its row does not read as one."""
    found = read_record(conn, initiative)
    if isinstance(found, Unreadable):
        raise UnreadableRecord(found)
    return found


def has_record(conn: Connection, initiative: str) -> bool:
    """Whether the initiative has a row, readable or not. A writer that must never write over a record asks this."""
    p = conn.dialect.placeholder
    return conn.query_one(f"SELECT 1 FROM landing_records WHERE initiative = {p}", (initiative,)) is not None


def put_record(conn: Connection, record: Mapping[str, Any]) -> None:
    """Write the whole record. `created_at` is kept from the first write."""
    row = (
        str(record["initiative"]), str(record.get("repo") or ""), str(record.get("state") or "building"),
        json_text(dict(record)), str(record.get("created_at") or record.get("updated_at") or ""),
        str(record.get("updated_at") or ""),
    )
    conn.execute(upsert(conn.dialect, "landing_records", _RECORD_COLUMNS, ("initiative",)), row)


def list_records(conn: Connection, states: Sequence[str] | None = None) -> Listing:
    """Every row, or those whose `state` column is in `states`, by initiative: the records, and apart from them the
    rows that do not read as one (`core.landing_record.listing`). One malformed row never stops the read of the rest."""
    rows = conn.query_all("SELECT initiative, state, record_json FROM landing_records ORDER BY initiative")
    return listing((str(initiative), raw) for initiative, state, raw in rows if states is None or state in states)


def delete_record(conn: Connection, initiative: str) -> None:
    p = conn.dialect.placeholder
    conn.execute(f"DELETE FROM landing_records WHERE initiative = {p}", (initiative,))


# -- notices: stalls told to the chair that no landing record can hold (migration 0019) ---------------------------

def told(conn: Connection, prefix: str) -> dict[str, str]:
    """Every notice whose key starts with `prefix`: key -> the reason it was told with."""
    rows = conn.query_all("SELECT key, reason FROM lander_notices ORDER BY key")
    return {str(key): str(reason) for key, reason in rows if str(key).startswith(prefix)}


def remember(conn: Connection, key: str, reason: str, now: str) -> None:
    """Record that the stall `key` was told with `reason`."""
    conn.execute(upsert(conn.dialect, "lander_notices", ("key", "reason", "since"), ("key",)), (key, reason, now))


def forget(conn: Connection, key: str) -> None:
    """The stall `key` has cleared: the next one is told again."""
    p = conn.dialect.placeholder
    conn.execute(f"DELETE FROM lander_notices WHERE key = {p}", (key,))


# -- land queue ---------------------------------------------------------------------------------------------------

def _item(row: Sequence[Any]) -> dict[str, Any]:
    return dict(zip(_ITEM_COLUMNS, row, strict=True))


def get_item(conn: Connection, ident: str) -> dict[str, Any] | None:
    p = conn.dialect.placeholder
    row = conn.query_one(f"SELECT {', '.join(_ITEM_COLUMNS)} FROM land_queue WHERE id = {p}", (ident,))
    return None if row is None else _item(row)


def list_items(conn: Connection, states: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Queue items oldest first, all of them or those in `states`."""
    rows = conn.query_all(f"SELECT {', '.join(_ITEM_COLUMNS)} FROM land_queue ORDER BY queued_at, id")
    return [item for item in map(_item, rows) if states is None or item["state"] in states]


def requeued_at(state: str, queued_at: str, now: str) -> str:
    """Pure. When an item that is queued again counts as queued from. A `failed` item keeps its first queueing, so an
    item that fails on every pass still ages, and the queue alarm sees it; one that was `done` is new work."""
    return queued_at if state == "failed" and queued_at else now


def enqueue(conn: Connection, kind: str, initiative: str, *, repo: str, now: str, phase: str = "", parent: str = "",
            reason: str = "") -> bool:
    """Queue one item. True when this call queued it: a new item, or one that was `done` or `failed`.

    An item already `queued` or `claimed` is left alone, so an event and the sweep may both name it. Queueing again
    keeps `attempts` and `last_error`, so a retried item still shows its history, and a `failed` item keeps its
    `queued_at` (`requeued_at`)."""
    ident = item_id(kind, initiative, phase)
    p = conn.dialect.placeholder
    with conn.transaction():
        found = get_item(conn, ident)
        if found is None:
            row = (ident, kind, initiative, phase, repo, parent, "queued", reason, now, "", "", "", 0, "", now)
            conn.execute(
                f"INSERT INTO land_queue ({', '.join(_ITEM_COLUMNS)}) VALUES ({', '.join([p] * len(_ITEM_COLUMNS))})", row
            )
            return True
        if found["state"] in ("queued", "claimed"):
            return False
        conn.execute(
            f"UPDATE land_queue SET state = 'queued', reason = {p}, queued_at = {p}, repo = {p}, parent = {p}, "
            f"claimed_by = '', claimed_at = '', lease_until = '', updated_at = {p} WHERE id = {p}",
            (reason, requeued_at(str(found["state"]), str(found.get("queued_at") or ""), now), repo, parent, now, ident),
        )
        return True


def claim(conn: Connection, ident: str, worker: str, *, now: str, lease_until: str) -> bool:
    """Take one item for `worker`. True only for the one caller that got it.

    A `queued` item is free. A `claimed` one is free again once its `lease_until` has passed: its worker died."""
    p = conn.dialect.placeholder
    changed = conn.execute(
        f"UPDATE land_queue SET state = 'claimed', claimed_by = {p}, claimed_at = {p}, lease_until = {p}, "
        f"attempts = attempts + 1, updated_at = {p} "
        f"WHERE id = {p} AND (state = 'queued' OR (state = 'claimed' AND lease_until < {p}))",
        (worker, now, lease_until, now, ident, now),
    )
    return changed == 1


def renew(conn: Connection, ident: str, worker: str, *, now: str, lease_until: str) -> bool:
    """Push a held claim's `lease_until` out. False when `worker` no longer holds the item."""
    p = conn.dialect.placeholder
    changed = conn.execute(
        f"UPDATE land_queue SET lease_until = {p}, updated_at = {p} WHERE id = {p} AND state = 'claimed' AND claimed_by = {p}",
        (lease_until, now, ident, worker),
    )
    return changed == 1


def _close(conn: Connection, ident: str, worker: str, state: str, error: str, now: str) -> bool:
    p = conn.dialect.placeholder
    changed = conn.execute(
        f"UPDATE land_queue SET state = {p}, last_error = {p}, lease_until = '', updated_at = {p} "
        f"WHERE id = {p} AND state = 'claimed' AND claimed_by = {p}",
        (state, error, now, ident, worker),
    )
    return changed == 1


def finish(conn: Connection, ident: str, worker: str, *, now: str) -> bool:
    """Close a claimed item as `done`. False when `worker` no longer holds it (its lease lapsed and another took it)."""
    return _close(conn, ident, worker, "done", "", now)


def fail(conn: Connection, ident: str, worker: str, *, now: str, error: str) -> bool:
    """Close a claimed item as `failed`, keeping why. It stays failed until something enqueues it again."""
    return _close(conn, ident, worker, "failed", error, now)


def release(conn: Connection, ident: str, worker: str, *, now: str, error: str = "") -> bool:
    """Hand a claimed item back to `queued` without closing it: the worker could not start it (a lease was busy)."""
    p = conn.dialect.placeholder
    changed = conn.execute(
        f"UPDATE land_queue SET state = 'queued', claimed_by = '', claimed_at = '', lease_until = '', last_error = {p}, "
        f"attempts = attempts - 1, updated_at = {p} WHERE id = {p} AND state = 'claimed' AND claimed_by = {p}",
        (error, now, ident, worker),
    )
    return changed == 1


def claimable(items: Sequence[Mapping[str, Any]], now: str) -> list[Mapping[str, Any]]:
    """Pure. The items a worker may claim at `now`, oldest first: queued, or claimed with a lapsed lease."""
    free = [i for i in items if i["state"] == "queued" or (i["state"] == "claimed" and str(i["lease_until"]) < now)]
    return sorted(free, key=lambda i: (str(i["queued_at"]), str(i["id"])))


def oldest_waiting(items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Pure. The longest-queued item not yet done: what the stall alarm reads."""
    waiting = [i for i in items if i["state"] in ("queued", "claimed", "failed")]
    return min(waiting, key=lambda i: (str(i["queued_at"]), str(i["id"])), default=None)
