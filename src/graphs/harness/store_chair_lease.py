"""A chair queue worker's kind lease (`chair:<kind>`), on the store's own clock, and the writes it fences (C3).

Every time here is the store's (`CURRENT_TIMESTAMP`, read inside the transaction that uses it), never the worker's,
so two machines whose clocks disagree still agree on who holds a kind. The rows and their epoch rule are
`store_lease`'s: a take or a steal moves the epoch on, a renew keeps it.

`record_fenced` is the fenced write: in one transaction it locks the lease row (`FOR UPDATE` on Postgres; sqlite's
`BEGIN IMMEDIATE` already serialises every writer), checks that the epoch is still the holder's and unexpired by the
store's clock, and only then inserts the chair action row. A takeover's UPDATE of the same lease row waits for that
commit, so a row is never written under an epoch another worker has already replaced.
"""

from __future__ import annotations

from collections.abc import Mapping

from harness import phase_pick, store_lease
from harness.store_cli import CHAIR_ACTION_COLUMNS, CHAIR_ACTION_KEY, action_row
from harness.store_dialect import POSTGRES, Connection, insert_ignore

__all__ = ["acquire", "holds", "leases", "record_fenced", "release", "renew", "store_now"]


def store_now(conn: Connection) -> str:
    """Edge. The store's clock as a `YYYY-MM-DDTHH:MM:SSZ` stamp."""
    found = conn.query_one(conn.dialect.now_sql)
    if found is None or found[0] is None:
        raise RuntimeError("the store returned no current time")
    return phase_pick.stamp_of(str(found[0]))


def acquire(conn: Connection, name: str, holder: str, ttl: int) -> int | None:
    """Edge. Take a free or expired lease (or move on one this holder already has), by the store's clock: the new
    epoch, or None when another holder's lease is live."""
    with conn.transaction():
        taken = store_lease.acquire(conn, name, holder, store_now(conn), ttl)
    return taken.epoch if taken.ok else None


def renew(conn: Connection, name: str, holder: str, epoch: int, ttl: int) -> bool:
    """Edge. Extend the lease from the store's clock; False once it has expired or another holder took it."""
    with conn.transaction():
        return store_lease.renew(conn, name, holder, epoch, store_now(conn), ttl)


def release(conn: Connection, name: str, holder: str, epoch: int) -> bool:
    with conn.transaction():
        return store_lease.release(conn, name, holder, epoch)


def _held_sql(conn: Connection) -> str:
    p = conn.dialect.placeholder
    lock = " FOR UPDATE" if conn.dialect is POSTGRES else ""
    return f"SELECT expires_at FROM leases WHERE name = {p} AND holder = {p} AND epoch = {p}{lock}"


def _held(conn: Connection, name: str, holder: str, epoch: int) -> bool:
    row = conn.query_one(_held_sql(conn), (name, holder, epoch))
    return row is not None and str(row[0]) > store_now(conn)


def holds(conn: Connection, name: str, holder: str, epoch: int) -> bool:
    """Edge. Whether `holder` still holds `name` at `epoch`, unexpired by the store's clock."""
    with conn.transaction():
        return _held(conn, name, holder, epoch)


def record_fenced(
    conn: Connection, name: str, lease_holder: str, epoch: int, holder: str, action: Mapping[str, object]
) -> bool:
    """Edge. Insert one chair action row only while `lease_holder` holds `name` at `epoch`, checked and written in one
    transaction with the lease row locked. False, with nothing written, once the lease is lost."""
    row = action_row(holder, action)
    with conn.transaction():
        if not _held(conn, name, lease_holder, epoch):
            return False
        conn.execute(insert_ignore(conn.dialect, "chair_actions", CHAIR_ACTION_COLUMNS, CHAIR_ACTION_KEY), list(row.values()))
    return True


def leases(conn: Connection, prefix: str) -> tuple[dict[str, str], str]:
    """Edge. Each lease whose name starts with `prefix`, to its expiry, and the store's clock, read together."""
    p = conn.dialect.placeholder
    with conn.transaction():
        rows = conn.query_all(f"SELECT name, expires_at FROM leases WHERE name LIKE {p}", (f"{prefix}%",))
        return {str(r[0]): str(r[1]) for r in rows}, store_now(conn)
