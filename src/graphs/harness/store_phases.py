"""The phase queue and the worker registry in the store: every read and write of `phase_queue` and `workers` (0020).

The chair writes a phase's row (`apply_changes`); a worker claims it (`claim_next`), keeps it (`heartbeat`), names its
run (`set_run`) and gives it back (`finish`, `release`). Every time is the store's own (`store_now`, read inside the
transaction that uses it), never a worker's clock. Every worker write is conditional on the claim it holds:
`claimed_by`, `claim_epoch`, state `claimed` and a lease not yet lapsed are in its WHERE clause, so a stale worker's
write matches no row (`phase_pick.fenced` is the same test, pure). Every write is safe to repeat: an insert is an
insert-ignore, an update names the state it moves from, and the registry row is an upsert of the worker's own row.

The rules (which row, how long, what a finished run means) are pure, in `harness.phase_pick`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from harness import phase_pick
from harness.store_dialect import Cell, Connection, Json, insert_ignore, json_load, json_text, upsert

__all__ = [
    "PHASE_COLUMNS",
    "WORKER_COLUMNS",
    "Claim",
    "apply_changes",
    "claim_next",
    "finish",
    "get_row",
    "heartbeat",
    "list_rows",
    "list_workers",
    "live_claims",
    "register",
    "release",
    "set_run",
    "store_now",
]

PHASE_COLUMNS: tuple[str, ...] = (
    "initiative", "phase", "kind", "repo", "base_ref", "branch", "requires_json", "state", "outcome", "reason", "band",
    "seq", "budget_usd", "queued_at", "ready_at", "claimed_by", "claim_epoch", "run_id", "claimed_at", "heartbeat_at",
    "lease_until", "attempts", "updated_at", "updated_by",
)
WORKER_COLUMNS: tuple[str, ...] = (
    "name", "capabilities_json", "lanes", "threads", "state", "version", "doctor_json", "doctor_at", "started_at", "beat_at",
)
_JSON_COLUMNS = frozenset({"requires_json", "capabilities_json", "doctor_json"})
_MAX_PAGES = 64  # pages of candidates one claim reads before it gives up: 1024 claimable rows ahead of the first it meets

type Row = dict[str, object]


@dataclass(frozen=True)
class Claim:
    """One claim a worker holds: the phase, the epoch every write of this claim carries, and the row as claimed."""

    initiative: str
    phase: str
    epoch: int
    lease_until: str
    row: Mapping[str, object]


def _decode(columns: Sequence[str], values: Sequence[Cell]) -> Row:
    return {c: json_load(v) if c in _JSON_COLUMNS else v for c, v in zip(columns, values, strict=True)}


def store_now(conn: Connection) -> str:
    """Edge. The store's clock as a stamp. Inside a transaction on Postgres this is the transaction's start time."""
    found = conn.query_one(conn.dialect.now_sql)
    if found is None or found[0] is None:
        raise RuntimeError("the store returned no current time")
    return phase_pick.stamp_of(str(found[0]))


def list_rows(conn: Connection) -> list[Row]:
    """Edge. Every queue row, in claim order."""
    rows = conn.query_all(f"SELECT {', '.join(PHASE_COLUMNS)} FROM phase_queue ORDER BY band, seq, initiative, phase")
    return [_decode(PHASE_COLUMNS, r) for r in rows]


def get_row(conn: Connection, initiative: str, phase: str) -> Row | None:
    """Edge. One phase's row, None when it has none."""
    p = conn.dialect.placeholder
    found = conn.query_one(
        f"SELECT {', '.join(PHASE_COLUMNS)} FROM phase_queue WHERE initiative = {p} AND phase = {p}", (initiative, phase)
    )
    return None if found is None else _decode(PHASE_COLUMNS, found)


# -- the chair's side ---------------------------------------------------------------------------------------------

def _new_row(change: Mapping[str, object], by: str, now: str) -> tuple[object, ...]:
    values: dict[str, object] = {
        "initiative": change["initiative"], "phase": change["phase"], "kind": change.get("kind") or "build",
        "repo": change.get("repo") or "", "base_ref": change.get("base_ref") or "", "branch": change.get("branch") or "",
        "requires_json": json_text(list(cast("Sequence[str]", change.get("requires") or []))), "state": "ready",
        "outcome": "", "reason": change.get("reason") or "", "band": change.get("band", 1), "seq": change.get("seq", 0),
        "budget_usd": change.get("budget_usd", 0.0), "queued_at": now, "ready_at": now, "claimed_by": "",
        "claim_epoch": 0, "run_id": "", "claimed_at": "", "heartbeat_at": "", "lease_until": "", "attempts": 0,
        "updated_at": now, "updated_by": by,
    }
    return tuple(values[c] for c in PHASE_COLUMNS)


def _apply(conn: Connection, change: Mapping[str, object], by: str, now: str) -> bool:
    """`insert` adds a ready row and leaves an existing one alone. `update` rewrites a waiting or ready row's
    requirements and order, never a claimed one's. `reopen` makes a finished or withdrawn row ready again, only while
    it is still the row the chair saw (same epoch), claimable at once with its attempts back at 0: work that came back
    is a new episode, and the last one's claims must not fail it before it runs. `withdraw` takes a waiting or ready row out of the queue (state
    `cancelled`: its phase is held, closed or over budget), never a claimed one. `exhaust` finishes as failed a row
    whose claims ran out while nobody holds it (`phase_pick.exhausted`, in SQL). Each is conditional on the state it
    moves from, so a repeat changes nothing."""
    p = conn.dialect.placeholder
    op, initiative, phase = change["op"], change["initiative"], change["phase"]
    requires = json_text(list(cast("Sequence[str]", change.get("requires") or [])))
    reason = change.get("reason") or ""
    if op == "insert":
        sql = insert_ignore(conn.dialect, "phase_queue", PHASE_COLUMNS, ("initiative", "phase"))
        return conn.execute(sql, _new_row(change, by, now)) == 1
    if op == "update":
        return conn.execute(
            f"UPDATE phase_queue SET requires_json = {p}, band = {p}, seq = {p}, repo = {p}, updated_at = {p}, "
            f"updated_by = {p} WHERE initiative = {p} AND phase = {p} AND state IN ('waiting', 'ready')",
            (requires, change.get("band", 1), change.get("seq", 0), change.get("repo") or "", now, by, initiative, phase),
        ) == 1
    if op == "reopen":
        return conn.execute(
            f"UPDATE phase_queue SET state = 'ready', outcome = '', reason = {p}, requires_json = {p}, band = {p}, "
            f"seq = {p}, ready_at = {p}, attempts = 0, updated_at = {p}, updated_by = {p} "
            f"WHERE initiative = {p} AND phase = {p} AND state IN ('finished', 'cancelled') AND claim_epoch = {p}",
            (reason, requires, change.get("band", 1), change.get("seq", 0), now, now, by,
             initiative, phase, change.get("epoch", 0)),
        ) == 1
    if op == "withdraw":
        return conn.execute(
            f"UPDATE phase_queue SET state = 'cancelled', reason = {p}, updated_at = {p}, updated_by = {p} "
            f"WHERE initiative = {p} AND phase = {p} AND state IN ('waiting', 'ready')",
            (reason, now, by, initiative, phase),
        ) == 1
    if op == "exhaust":
        return conn.execute(
            f"UPDATE phase_queue SET state = 'finished', outcome = 'failed', reason = {p}, lease_until = '', "
            f"updated_at = {p}, updated_by = {p} WHERE initiative = {p} AND phase = {p} AND attempts >= {p} "
            f"AND (state = 'ready' OR (state = 'claimed' AND lease_until < {p}))",
            (reason, now, by, initiative, phase, phase_pick.MAX_ATTEMPTS, now),
        ) == 1
    raise ValueError(f"unknown phase queue change {op!r}")


def apply_changes(conn: Connection, changes: Sequence[Mapping[str, object]], by: str) -> int:
    """Edge. The chair's changes (`chair_phase_queue.changes`) in one transaction at the store's time; the rows changed."""
    with conn.transaction():
        now = store_now(conn)
        return sum(_apply(conn, change, by, now) for change in changes)


# -- the worker's side --------------------------------------------------------------------------------------------

def claim_next(conn: Connection, worker: str, capabilities: Sequence[str], free: int) -> Claim | None:
    """Edge. Claim the next phase `worker` qualifies for, in one transaction; None when it has no free lane or none.

    Claimable rows are read a page of `phase_pick.CANDIDATES` at a time in (band, seq) order, without a lock, and
    `phase_pick.choices` keeps those the worker meets; a page with none it meets is paged past, so phases nobody here
    can run never starve the rest. Each choice is then locked alone, `FOR UPDATE SKIP LOCKED` on Postgres (sqlite's
    BEGIN IMMEDIATE serialises instead), re-checked as claimable, and taken by one conditional UPDATE: the epoch goes
    up by one and the attempt is counted. A row another worker holds locked, or claimed since the read, is skipped
    for the next, so two claimers never wait on each other and never take the same row."""
    if free <= 0:
        return None
    p = conn.dialect.placeholder
    claimable_sql = (
        f"((state = 'ready' AND ready_at <= {p}) OR (state = 'claimed' AND lease_until < {p})) AND attempts < {p}"
    )
    with conn.transaction():
        now = store_now(conn)
        page_args = (now, now, phase_pick.MAX_ATTEMPTS)
        for offset in range(0, _MAX_PAGES * phase_pick.CANDIDATES, phase_pick.CANDIDATES):
            page = conn.query_all(
                f"SELECT {', '.join(PHASE_COLUMNS)} FROM phase_queue WHERE {claimable_sql} "
                f"ORDER BY band, seq, initiative, phase LIMIT {phase_pick.CANDIDATES} OFFSET {offset}",
                page_args,
            )
            for chosen in phase_pick.choices([_decode(PHASE_COLUMNS, r) for r in page], capabilities, free, now):
                initiative, phase = str(chosen["initiative"]), str(chosen["phase"])
                locked = conn.query_one(
                    f"SELECT initiative FROM phase_queue WHERE initiative = {p} AND phase = {p} AND {claimable_sql}"
                    f"{conn.dialect.skip_locked}",
                    (initiative, phase, *page_args),
                )
                if locked is None:
                    continue
                until = phase_pick.lease(now)
                taken = conn.execute(
                    f"UPDATE phase_queue SET state = 'claimed', claimed_by = {p}, claim_epoch = claim_epoch + 1, "
                    f"run_id = '', claimed_at = {p}, heartbeat_at = {p}, lease_until = {p}, attempts = attempts + 1, "
                    f"updated_at = {p}, updated_by = {p} WHERE initiative = {p} AND phase = {p} AND {claimable_sql}",
                    (worker, now, now, until, now, worker, initiative, phase, *page_args),
                )
                row = get_row(conn, initiative, phase) if taken == 1 else None
                if row is not None:
                    epoch = row.get("claim_epoch")
                    return Claim(initiative, phase, epoch if isinstance(epoch, int) else 0, until, row)
            if len(page) < phase_pick.CANDIDATES:
                return None
    return None


def _fence(p: str) -> str:
    return (f"WHERE initiative = {p} AND phase = {p} AND claimed_by = {p} AND claim_epoch = {p} "
            f"AND state = 'claimed' AND lease_until > {p}")


def heartbeat(conn: Connection, claim: Claim, worker: str) -> str | None:
    """Edge. Renew the claim's lease from the store's clock; the new lease, or None when the claim is gone (another
    worker holds it, or it lapsed). A lapsed lease is never revived."""
    p = conn.dialect.placeholder
    with conn.transaction():
        now = store_now(conn)
        until = phase_pick.lease(now)
        changed = conn.execute(
            f"UPDATE phase_queue SET heartbeat_at = {p}, lease_until = {p} {_fence(p)}",
            (now, until, claim.initiative, claim.phase, worker, claim.epoch, now),
        )
    return until if changed == 1 else None


def set_run(conn: Connection, claim: Claim, worker: str, run_id: str) -> bool:
    """Edge. Name the run this claim started; False when the claim is gone."""
    p = conn.dialect.placeholder
    with conn.transaction():
        now = store_now(conn)
        return conn.execute(
            f"UPDATE phase_queue SET run_id = {p}, updated_at = {p} {_fence(p)}",
            (run_id, now, claim.initiative, claim.phase, worker, claim.epoch, now),
        ) == 1


def finish(conn: Connection, claim: Claim, worker: str, outcome: str, reason: str) -> bool:
    """Edge. Close the claim as `finished` with `outcome` (approved, quarantined or failed): the ack, or a permanent
    nack. False when the claim is gone, so a stale worker's finish changes nothing."""
    p = conn.dialect.placeholder
    with conn.transaction():
        now = store_now(conn)
        return conn.execute(
            f"UPDATE phase_queue SET state = 'finished', outcome = {p}, reason = {p}, lease_until = '', "
            f"updated_at = {p}, updated_by = {p} {_fence(p)}",
            (outcome, reason, now, worker, claim.initiative, claim.phase, worker, claim.epoch, now),
        ) == 1


def release(conn: Connection, claim: Claim, worker: str, reason: str, wait_s: int, *, refund: bool = False) -> bool:
    """Edge. The retryable nack: give the claim back as `ready`, claimable again `wait_s` seconds from now. The
    attempt stays counted (the claim counted it) unless `refund`: a refused launch, or a worker told to stop, costs
    the phase nothing. The epoch stays, so the next claim is a new epoch."""
    p = conn.dialect.placeholder
    back = ", attempts = attempts - 1" if refund else ""
    with conn.transaction():
        now = store_now(conn)
        return conn.execute(
            f"UPDATE phase_queue SET state = 'ready', claimed_by = '', lease_until = '', reason = {p}, ready_at = {p}, "
            f"updated_at = {p}, updated_by = {p}{back} {_fence(p)}",
            (reason, phase_pick.lease(now, wait_s), now, worker, claim.initiative, claim.phase, worker, claim.epoch, now),
        ) == 1


def live_claims(conn: Connection) -> list[Row]:
    """Edge. Every claim whose lease has not lapsed by the store's clock, any worker's."""
    p = conn.dialect.placeholder
    now = store_now(conn)
    rows = conn.query_all(
        f"SELECT {', '.join(PHASE_COLUMNS)} FROM phase_queue WHERE state = 'claimed' AND lease_until > {p} "
        "ORDER BY band, seq, initiative, phase",
        (now,),
    )
    return [_decode(PHASE_COLUMNS, r) for r in rows]


# -- the registry -------------------------------------------------------------------------------------------------

def register(conn: Connection, name: str, capabilities: Sequence[str], lanes: int, state: str, version: str,
             doctor: Json) -> Row:
    """Edge. Write the worker's own row: what it can run and how many lanes, stamped by the store's clock. `started_at`
    is kept from the first write while the worker stays up, so a re-registration after a doctor run is a repeat."""
    p = conn.dialect.placeholder
    with conn.transaction():
        now = store_now(conn)
        found = conn.query_one(f"SELECT state, started_at FROM workers WHERE name = {p}", (name,))
        started = str(found[1]) if found is not None and found[0] == "up" and state == "up" and found[1] else now
        row: Row = {
            "name": name, "capabilities_json": sorted(set(capabilities)), "lanes": lanes, "threads": 0, "state": state,
            "version": version, "doctor_json": doctor, "doctor_at": now, "started_at": started, "beat_at": now,
        }
        conn.execute(
            upsert(conn.dialect, "workers", WORKER_COLUMNS, ("name",)),
            [json_text(row[c]) if c in _JSON_COLUMNS else row[c] for c in WORKER_COLUMNS],
        )
    return row


def list_workers(conn: Connection) -> list[Row]:
    """Edge. Every registered worker, by name."""
    rows = conn.query_all(f"SELECT {', '.join(WORKER_COLUMNS)} FROM workers ORDER BY name")
    return [_decode(WORKER_COLUMNS, r) for r in rows]
