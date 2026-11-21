"""Edge. The facts that say who plans a kind of the chair's work: the profile's `chair.queues`, and the kinds' worker
leases with the store's clock (`chair_kinds`). The tick and every queue worker read them each pass, the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_kinds, route, run_store
from agent_tools.chair_types import QueueLeases
from core.jsontypes import is_mapping

__all__ = [
    "NOW_SQL", "SEEN_MARKER", "QueueLeasesReader", "mark_seen", "queue_lease_holds", "queues_keeping_last_good", "read_chair_queues",
    "read_queue_leases", "seen_marker", "store_stamp",
]

LEASE_CHECK_SECONDS = 5.0  # a lease check that has not answered by then reads as not held, never as held


def queue_lease_holds(runs_dir: Path, proof: chair_kinds.QueueProof, seconds: float = LEASE_CHECK_SECONDS) -> bool:
    """Edge. Whether the store says `proof`'s kind lease is held by its holder at its epoch, unexpired by the store's
    clock (`store_chair_lease.holds`). A store that fails, hangs past `seconds` or is missing reads as not held."""
    from agent_tools.chair_queue import bounded
    from harness import store_chair_lease
    from harness.store_dialect import connect

    def check() -> bool:
        conn = connect(run_store.store_url_for(runs_dir))
        try:
            return store_chair_lease.holds(conn, proof.lease, proof.holder, proof.epoch)
        finally:
            conn.close()

    return bounded(check, seconds, False)


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


NOW_SQL = {  # the bind token tells the engine apart, as everywhere in run_store
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
            now = conn.execute(NOW_SQL[token]).fetchone()["now"]
        finally:
            conn.close()
    except Exception:  # unknown, not empty: the tick then leaves every queue kind alone this tick
        return None, ""
    leases = {str(r["name"]).removeprefix(chair_kinds.LEASE_PREFIX): str(r["expires_at"]) for r in rows}
    return leases, store_stamp(now)


# Under the runs directory: present once any tick on this machine has seen a `chair:<kind>` lease row. A restarted tick
# reads it, so a failed lease read still fails closed after a restart; nothing ever removes it but a person.
SEEN_MARKER = "chair-queue-lease-seen"


def seen_marker(runs_dir: Path) -> Path:
    """Pure. The marker that says a worker lease row has been seen on this machine."""
    return Path(runs_dir) / SEEN_MARKER


def mark_seen(runs_dir: Path) -> bool:
    """Edge. Write the marker (an empty file; writing it twice changes nothing). False when it could not be written: the
    reader then still remembers it for the life of its process."""
    try:
        seen_marker(runs_dir).touch(exist_ok=True)
    except OSError:
        return False
    return True


class QueueLeasesReader:
    """Edge. `read_queue_leases` with what the tick needs to fail closed sensibly: whether a worker lease row has ever
    been seen (in this process, or by any earlier one on this machine: `SEEN_MARKER`), and since when (on `clock`,
    monotonic seconds) the read has been failing. The tick's facts, its pre-perform re-read and its per-action check
    share one reader."""

    def __init__(self, runs_dir: Path, clock: Callable[[], float]) -> None:
        self._runs_dir, self._clock = runs_dir, clock
        self._seen = seen_marker(runs_dir).exists()
        self._failing_since: float | None = None

    def __call__(self) -> QueueLeases:
        leases, now = read_queue_leases(self._runs_dir)
        if leases is None:
            at = self._clock()
            self._failing_since = at if self._failing_since is None else self._failing_since
            return QueueLeases(None, "", self._seen, at - self._failing_since)
        if leases and not self._seen:
            mark_seen(self._runs_dir)
            self._seen = True
        self._failing_since = None
        return QueueLeases(leases, now, self._seen, 0.0)

    def held(self, kind: str) -> bool:
        """Whether a worker holds `kind`'s lease now, by the store's clock. Unreadable: held when a worker could be
        running (a lease row has been seen), so the tick leaves the action to it; else not."""
        read = self()
        if read.leases is None:
            return read.seen
        return kind in chair_kinds.live_kinds(read.leases, read.now)
