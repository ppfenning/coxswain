"""The landing record and the land queue in the store: every read and write of the two tables of migration 0016.

Each function takes an open `Connection`. Times are UTC strings (`%Y-%m-%dT%H:%M:%SZ`), which compare in order as
text. A queue item moves `queued -> claimed -> done | failed`, and back to `queued` when it is enqueued again or
its failed attempt is to be retried; a `claimed` item whose `lease_until` has passed may be claimed again.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harness.store_dialect import Connection, json_load, json_text, upsert

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

def get_record(conn: Connection, initiative: str) -> dict[str, Any] | None:
    p = conn.dialect.placeholder
    row = conn.query_one(f"SELECT record_json FROM landing_records WHERE initiative = {p}", (initiative,))
    return None if row is None else dict(json_load(row[0]) or {})


def put_record(conn: Connection, record: Mapping[str, Any]) -> None:
    """Write the whole record. `created_at` is kept from the first write."""
    row = (
        str(record["initiative"]), str(record.get("repo") or ""), str(record.get("state") or "building"),
        json_text(dict(record)), str(record.get("created_at") or record.get("updated_at") or ""),
        str(record.get("updated_at") or ""),
    )
    conn.execute(upsert(conn.dialect, "landing_records", _RECORD_COLUMNS, ("initiative",)), row)


def list_records(conn: Connection, states: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Every record, or those in `states`, by initiative."""
    rows = conn.query_all("SELECT state, record_json FROM landing_records ORDER BY initiative")
    return [dict(json_load(raw) or {}) for state, raw in rows if states is None or state in states]


def delete_record(conn: Connection, initiative: str) -> None:
    p = conn.dialect.placeholder
    conn.execute(f"DELETE FROM landing_records WHERE initiative = {p}", (initiative,))


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


def enqueue(conn: Connection, kind: str, initiative: str, *, repo: str, now: str, phase: str = "", parent: str = "",
            reason: str = "") -> bool:
    """Queue one item. True when this call queued it: a new item, or one that was `done` or `failed`.

    An item already `queued` or `claimed` is left alone, so an event and the sweep may both name it. Queueing again
    keeps `attempts` and `last_error`, so a retried item still shows its history."""
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
            (reason, now, repo, parent, now, ident),
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
