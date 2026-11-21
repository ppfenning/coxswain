"""Edge. The facts that say who plans a kind of the chair's work: the profile's `chair.queues`, and the kinds' worker
leases with the store's clock (`chair_kinds`). The tick and every queue worker read them each pass, the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_kinds, route, run_store
from core.jsontypes import is_mapping

__all__ = ["queues_keeping_last_good", "read_chair_queues", "read_queue_leases", "store_stamp"]


def read_chair_queues(profile_path: Path) -> frozenset[str] | route.ProfileError:
    """The kinds the profile's `chair.queues` lists. A missing profile lists none; one that will not parse comes back
    as the `ProfileError`, so the caller can keep its last good read."""
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    try:
        chair: object = route.parse_profile(text).get("chair")
    except route.ProfileError as exc:
        return exc
    return chair_kinds.queued_kinds(chair.get("queues") if is_mapping(chair) else None)


def queues_keeping_last_good(profile_path: Path) -> Callable[[], frozenset[str]]:
    """A reader of `chair.queues` that re-reads the profile on every call. A read that fails keeps the last good value,
    and the first read that fails moves nothing (the default, none): an edit half saved never flips a kind."""
    good: list[frozenset[str]] = [frozenset()]  # edge state: the last good read

    def read() -> frozenset[str]:
        value = read_chair_queues(profile_path)
        if not isinstance(value, route.ProfileError):
            good[0] = value
        return good[0]

    return read


def store_stamp(raw: object) -> str:
    """Pure. The store's clock text (`2026-11-20 10:00:00.123456`, or a `T` form) as a `YYYY-MM-DDTHH:MM:SSZ` stamp."""
    text = str(raw).strip().replace("T", " ")[:19]
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_NOW_SQL = {  # the bind token tells the engine apart, as everywhere in run_store
    "%s": "SELECT CAST(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' AS TEXT) AS now",
    "?": "SELECT CURRENT_TIMESTAMP AS now",
}


def read_queue_leases(runs_dir: Path) -> tuple[dict[str, str] | None, str]:
    """Each kind's worker lease expiry and the store's clock, from one read-only connection; a missing sqlite store is
    no lease. Any failure is (None, ""), which the tick reads as unknown and fails closed on (`chair_kinds`)."""
    try:
        opened = run_store.open_store(runs_dir)
        if opened is None:
            return {}, ""
        conn, token = opened
        try:
            sql = run_store.sql_for("SELECT name, expires_at FROM leases WHERE name LIKE {p}", token)
            rows = conn.execute(sql, (f"{chair_kinds.LEASE_PREFIX}%",)).fetchall()
            now = conn.execute(_NOW_SQL[token]).fetchone()["now"]
        finally:
            conn.close()
    except Exception:  # unknown, not empty: the tick then leaves every queue kind alone this tick
        return None, ""
    leases = {str(r["name"]).removeprefix(chair_kinds.LEASE_PREFIX): str(r["expires_at"]) for r in rows}
    return leases, store_stamp(now)
