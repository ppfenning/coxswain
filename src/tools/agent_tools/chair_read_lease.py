"""The `lease` source of `chair_facts.FactsDeps`: the store row named `chair.LEASE_NAME`, or `ABSENT` when there is none."""
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_tools import chair, run_store
from agent_tools.chair import LEASE_NAME

Row = dict[str, Any]

# FactsDeps names no absent value. `{}` would read as held by nobody, and `chair_plan._lease_gate` would stand by forever.
# A lease never taken reads as released, so the gate plans `take_lease`.
ABSENT: Mapping[str, Any] = MappingProxyType({"holder": "", "host": "", "epoch": 0, "released": True, "stale": False})

# The released shape in this repo's own fixtures keeps the holder and expires the row at the Unix epoch
# (tests/test_epic_and_cli.py). A null holder is the shape of `store_cli.parse_lease`'s released reply. Both read as released.
_RELEASED_AT = datetime(1970, 1, 1, tzinfo=UTC)


def _expiry(expires_at: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _host(holder: str) -> str:
    """The host in a `session@host:pid` holder; empty for any other shape."""
    return holder.partition("@")[2].rpartition(":")[0]


def lease_record(row: Mapping[str, Any] | None, now: datetime) -> Row:
    """Stale is an expiry at or before `now`, or one that does not parse."""
    if row is None:
        return dict(ABSENT)
    holder = row["holder"] or ""
    expiry = _expiry(row["expires_at"])
    return {
        "holder": holder,
        "host": _host(holder),
        "epoch": int(row.get("epoch") or 0),
        "released": not holder or expiry == _RELEASED_AT,
        "stale": expiry is None or expiry <= now,
    }


def read_lease(runs_dir: Path, now: datetime, *, pid_alive: Callable[[int], bool] = chair.pid_alive, host: str | None = None) -> Row:
    """Edge. `ABSENT` with no store, no row, or an unreadable store. `host` defaults to this machine's name."""
    # `run_store.lease` asks for a `runs:<prefix>` name, so the read reuses its private `_open`. Only holder and expires_at are
    # known columns (`run_store._lease_table` selects them); `SELECT *` keeps a missing epoch column from reading as no lease.
    opened = run_store._open(runs_dir)
    if opened is None:
        return dict(ABSENT)
    conn, mark = opened
    try:
        row = conn.execute(f"SELECT * FROM leases WHERE name = {mark}", (LEASE_NAME,)).fetchone()
    except run_store._DB_ERRORS:
        return dict(ABSENT)
    finally:
        conn.close()
    return _with_takeover(lease_record(None if row is None else dict(row), now), runs_dir, now, pid_alive=pid_alive, host=host)


def _lent(lease: Row, record: Mapping[str, Any]) -> bool:
    """The holder's own chair.json record, already matched to the lease, marks the routine ticks lent. A released lease lends nothing."""
    return not lease["released"] and record.get("routine_lent") is True


def _session_held(lease: Row, record: Mapping[str, Any]) -> bool:
    """A matched record with no `until`, no `taken_over_from` stamp and no lent routine ticks: a session that took the lease informally."""
    return not lease["released"] and "until" not in record and "taken_over_from" not in record and record.get("routine_lent") is not True


def _with_takeover(
    lease: Row, runs_dir: Path, now: datetime, *, pid_alive: Callable[[int], bool] = chair.pid_alive, host: str | None = None
) -> Row:
    """Edge. Adds `until` and `expired` from chair.json when it is this lease holder's record and carries an `until`.
    Adds `lent` when that record is the holder's and its `routine_lent` is true, with or without an `until`.
    Adds `session_held` when that record is the holder's and carries no `until`, `taken_over_from` or lent routine ticks.
    A session or thief holder never renews its store lease, so a record that reads live and unexpired clears `stale`.
    The record carries no heartbeat window, so `chair.liveness` uses its default. A released lease stays released."""
    try:
        record = chair.read(runs_dir)
    except (OSError, ValueError):
        return lease
    if record is None:
        return lease
    if chair.lease_holder(str(record.get("session")), record.get("pid"), str(record.get("host"))) != lease["holder"]:
        return lease
    lent = {"lent": True} if _lent(lease, record) else {}
    if "until" not in record:
        return {**lease, **lent, **({"session_held": True} if _session_held(lease, record) else {})}
    expired = chair.expired(record, now)
    taken = {**lease, "until": str(record["until"]), "expired": expired, **lent}
    pid = record.get("pid")
    if expired or lease["released"] or not isinstance(pid, int):
        return taken
    local = socket.gethostname() if host is None else host
    return {**taken, "stale": False} if chair.liveness(record, pid_alive(pid), now, local) == "live" else taken
