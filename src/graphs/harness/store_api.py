"""The store in process: typed reads and writes that agent_tools calls directly and the store CLIs wrap.

Two layers. Each `*_on` function takes an open Connection and returns a typed value; `harness.store_cli` calls
them with the connection it opened, so the CLI and an in-process caller share one code path. Each url-level function
opens the store, calls one `*_on`, and closes it. A store that cannot be opened is `NotAvailable`, one that fails a
statement is `Failed`; neither raises. Reads open read-only (`store_read.connect_readonly`), so a read never creates
a store; writes open through `open_store`, which migrates. Nothing here reads the clock: `now` is an argument.

Every value at this surface is a frozen dataclass, a TypedDict or JSON (`Json`), so a caller in another package
sees what it gets without reading a CLI's stdout.
"""

from __future__ import annotations

import functools
import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict, cast, get_args

from harness import store_lease, store_queue
from harness.store_dialect import Connection, StoreDriverMissing, json_load, json_text
from harness.store_landed import mark_landed
from harness.store_migrate import MigrationError, open_store
from harness.store_pause import clear_paused, set_paused
from harness.store_read import StoreVersionError, connect_readonly
from harness.store_work_state import Mismatch, set_state
from harness.store_write import record_merge

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None
type JsonObject = dict[str, Json]

# Every state a work item can be in, the one enum `core.workstore.STATES` spells out (a test holds them equal).
WorkState = Literal["todo", "ready", "in_progress", "approved", "blocked", "done", "dropped", "quarantined"]
WORK_STATES: tuple[WorkState, ...] = get_args(WorkState)
# The CLIs' exit for a store that could not be read or written; `Failed.code` carries it so a message reads the same.
EXIT_STORE_ERROR = 2



@functools.cache
def db_errors() -> tuple[type[BaseException], ...]:
    """What a failed statement raises. The postgres driver is an optional extra, imported on first use, so importing
    this module stays cheap for a `cox` command that never opens the store."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover - depends on what is installed
        return (sqlite3.Error,)
    return (sqlite3.Error, psycopg.Error)


def open_errors() -> tuple[type[BaseException], ...]:
    """What opening raises: ValueError on an unknown scheme (or a sqlite store outside the suites), OSError on a bad
    path. Caught around opening only, so a handler bug still raises with its traceback."""
    return (*db_errors(), ValueError, OSError, StoreDriverMissing, MigrationError, StoreVersionError)


class WorkItemRecord(TypedDict):
    """A work item as a state write left it."""

    initiative: str
    task_id: str
    phase: str
    state: str
    needs: list[str]
    updated_at: str
    updated_by: str


class WorkItemRow(TypedDict):
    """One `work_items` row as stored; `needs_json` is the cell as the driver returns it."""

    initiative: str
    task_id: str
    phase: str | None
    state: str
    needs_json: Json
    updated_at: str | None
    updated_by: str | None


class QueueRow(TypedDict):
    """One `work_items` row with its queue and claim columns, the JSON columns decoded."""

    initiative: str
    task_id: str
    kind: str | None
    phase: str | None
    state: str
    needs: Json
    title: str | None
    surfaces: Json
    body: str | None
    extra: Json
    priority: int | None
    issue_number: int | None
    project_item_id: str | None
    parent_item: str | None
    updated_at: str | None
    updated_by: str | None
    holder: str | None
    epoch: int | None
    expires_at: str | None


class Claim(TypedDict):
    holder: str
    epoch: int
    expires_at: str


@dataclass(frozen=True)
class NotAvailable:
    """The store could not be opened: no driver, an unknown scheme, an unreachable server, a schema mismatch."""

    reason: str = ""


@dataclass(frozen=True)
class Failed:
    """The store opened but a statement failed, or the input was refused. `code` is the CLI exit it maps to."""

    code: int
    detail: str


@dataclass(frozen=True)
class StateSet:
    record: WorkItemRecord


@dataclass(frozen=True)
class StateRefused:
    """A write the store refused: `current` is the row's state when a compare-and-set missed, None with no row."""

    detail: str
    current: str | None = None


@dataclass(frozen=True)
class Landed:
    record: JsonObject


@dataclass(frozen=True)
class NotInStore:
    pass


@dataclass(frozen=True)
class MergeRecorded:
    rows: int


@dataclass(frozen=True)
class LeaseGranted:
    epoch: int
    holder: str


@dataclass(frozen=True)
class LeaseRefused:
    epoch: int | None
    holder: str | None


@dataclass(frozen=True)
class LeaseReleased:
    pass


@dataclass(frozen=True)
class LeaseError:
    detail: str


@dataclass(frozen=True)
class Paused:
    reason: str | None


@dataclass(frozen=True)
class PauseRefused:
    pass


@dataclass(frozen=True)
class Resumed:
    pass


@dataclass(frozen=True)
class ResumeRefused:
    pass


@dataclass(frozen=True)
class Upserted:
    """`changed` is False when the row already held this content."""

    changed: bool


@dataclass(frozen=True)
class Claimed:
    claim: Claim


@dataclass(frozen=True)
class Held:
    """A live claim by another holder."""


@dataclass(frozen=True)
class Released:
    """`cleared` is False when the holder did not hold the row."""

    cleared: bool


@dataclass(frozen=True)
class Resolved:
    """What a short id or an old key names: `kind` is "work_item" (key `initiative/task`) or "run" (key the run id)."""

    kind: str
    key: str


@dataclass(frozen=True)
class TableStamp:
    """A version of a `work_items` slice: two equal stamps mean no row in it changed in between."""

    count: int
    newest: str | None
    digest: str


SetStateResult = StateSet | StateRefused | Failed | NotAvailable
MarkLandedResult = Landed | NotInStore | Failed | NotAvailable
MergeResult = MergeRecorded | StateRefused | Failed | NotAvailable
LeaseResult = LeaseGranted | LeaseReleased | LeaseRefused | LeaseError | NotAvailable
PauseResult = Paused | PauseRefused | Failed | NotAvailable
ResumeResult = Resumed | ResumeRefused | Failed | NotAvailable


# --- pure ------------------------------------------------------------------------------------------------------------


def refusal_detail(task: str, current: str | None, expected: str) -> str:
    """The one line a missed compare-and-set reads as, in the CLI and in process alike."""
    return f"work item {task} is in state {current or 'no row'}, expected {expected}"


def no_row_detail(initiative: str, task: str) -> str:
    return f"no work item {task} in initiative {initiative} and --phase was not given"


def state_problem(state: str, expected: str | None) -> str | None:
    """Pure. Why a state write is refused before it reaches the store: a target or an expected state that is not a work
    state. None when both are (`expected` may be None). The type says WorkState; a caller's `str(...)` can still lie."""
    unknown = [s for s in (state, expected) if s is not None and s not in WORK_STATES]
    return f"unknown work state {unknown[0]!r}; expected one of {', '.join(WORK_STATES)}" if unknown else None


def stamp_of(rows: Sequence[Sequence[Json]], updated_at_index: int) -> TableStamp:
    """Pure: the stamp of rows read in key order; the digest covers every column, since a queue upsert keeps an older
    updated_at on a changed row, so count and greatest updated_at alone miss ordinary writes."""
    newest = max((str(r[updated_at_index]) for r in rows if r[updated_at_index] is not None), default=None)
    return TableStamp(len(rows), newest, hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest())


def phase_done_record(record: JsonObject, branch: str) -> JsonObject:
    """Pure: a phase record whose manifest says done and merged into `branch`, its landed mark dropped."""
    manifest = record.get("manifest_record")
    kept = {k: v for k, v in manifest.items() if k != "landed"} if isinstance(manifest, dict) else {}
    return {**record, "manifest_record": {**kept, "status": "done", "merged_into": branch}}


# --- on an open connection -------------------------------------------------------------------------------------------


def set_state_on(
    conn: Connection,
    initiative: str,
    task: str,
    state: WorkState,
    by: str,
    now: str,
    *,
    phase: str | None = None,
    expected: WorkState | None = None,
) -> StateSet | StateRefused:
    """Move one work item; with `expected`, only when its state equals it (compare-and-set in the write itself)."""
    row = set_state(conn, initiative, task, state, by, phase, now, expected=expected)
    if isinstance(row, Mismatch):
        return StateRefused(refusal_detail(task, row.current, expected or ""), row.current)
    if row is None:
        return StateRefused(no_row_detail(initiative, task))
    return StateSet(
        WorkItemRecord(
            initiative=row["initiative"], task_id=row["task_id"], phase=row["phase"], state=row["state"],
            needs=list(row["needs"]), updated_at=row["updated_at"], updated_by=row["updated_by"],
        )
    )  # fmt: skip


def mark_landed_on(conn: Connection, run_id: str, phase: str, task: str, pr: str, at: str) -> Landed | NotInStore:
    record = mark_landed(conn, run_id, phase, task, pr, at)
    return NotInStore() if record is None else Landed(record)


def record_merge_on(conn: Connection, initiative: str, task: str, commit: str, by: str, now: str) -> MergeRecorded | StateRefused:
    """Stamp `merged_commit` on an approved row; 0 rows for no row or the same commit again. Any other state is refused."""
    try:
        return MergeRecorded(record_merge(conn, initiative, task, commit, now, by))
    except ValueError as exc:
        return StateRefused(str(exc))


def record_phase_done_on(conn: Connection, run_id: str, phase: str, branch: str) -> MergeRecorded | StateRefused:
    """Mark the phase row's manifest done and merged into `branch`; refused when the phase has no row."""
    mark = conn.dialect.placeholder
    with conn.transaction():
        row = conn.query_one(f"SELECT record_json FROM phases WHERE run_id = {mark} AND phase_id = {mark}", (run_id, phase))
        if row is None:
            return StateRefused(f"no phase row {run_id}:{phase}")
        record = phase_done_record(json_load(row[0]) or {}, branch)
        sql = f"UPDATE phases SET record_json = {mark} WHERE run_id = {mark} AND phase_id = {mark}"
        conn.execute(sql, (json_text(record), run_id, phase))
    return MergeRecorded(1)


def lease_acquire_on(conn: Connection, name: str, holder: str, now: str, ttl: int, *, steal: bool = False) -> LeaseGranted | LeaseRefused:
    taken = store_lease.acquire(conn, name, holder, now, ttl, steal=steal)
    if taken.ok and taken.epoch is not None and taken.holder is not None:
        return LeaseGranted(taken.epoch, taken.holder)
    return LeaseRefused(taken.epoch, taken.holder)


def lease_renew_on(conn: Connection, name: str, holder: str, epoch: int, now: str, ttl: int) -> LeaseGranted | LeaseRefused:
    """A renew keeps the epoch, so a granted renew is the caller's own epoch and holder."""
    return LeaseGranted(epoch, holder) if store_lease.renew(conn, name, holder, epoch, now, ttl) else LeaseRefused(None, None)


def lease_release_on(conn: Connection, name: str, holder: str, epoch: int) -> LeaseReleased | LeaseRefused:
    return LeaseReleased() if store_lease.release(conn, name, holder, epoch) else LeaseRefused(None, None)


def run_exists_on(conn: Connection, run_id: str) -> bool:
    p = conn.dialect.placeholder
    return conn.query_one(f"SELECT 1 FROM runs WHERE run_id = {p}", (run_id,)) is not None


def pause_on(conn: Connection, run_id: str, now: str, reason: str | None = None) -> Paused | PauseRefused:
    if not run_exists_on(conn, run_id):
        return PauseRefused()
    set_paused(conn, run_id, now)
    return Paused(reason)


def resume_on(conn: Connection, run_id: str) -> Resumed | ResumeRefused:
    if not run_exists_on(conn, run_id):
        return ResumeRefused()
    clear_paused(conn, run_id)
    return Resumed()


_WORK_ITEM_COLUMNS = ("initiative", "task_id", "phase", "state", "needs_json", "updated_at", "updated_by")
# Columns a stamp or a read may narrow by; fixed names, so no caller text reaches the SQL.
_FILTERS = frozenset({"initiative", "kind"})


def _where(conn: Connection, filters: Sequence[tuple[str, str | None]]) -> tuple[str, list[str]]:
    given = [(column, value) for column, value in filters if value is not None]
    if any(column not in _FILTERS for column, _ in given):
        raise ValueError(f"cannot narrow work_items by {[c for c, _ in given]}")
    p = conn.dialect.placeholder
    clause = " WHERE " + " AND ".join(f"{column} = {p}" for column, _ in given) if given else ""
    return clause, [value for _, value in given]


def work_item_rows_on(conn: Connection, initiative: str | None = None) -> list[WorkItemRow]:
    where, params = _where(conn, [("initiative", initiative)])
    rows = conn.query_all(f"SELECT {', '.join(_WORK_ITEM_COLUMNS)} FROM work_items{where}", params)
    return [
        WorkItemRow(
            initiative=r[0], task_id=r[1], phase=r[2], state=r[3], needs_json=r[4], updated_at=r[5], updated_by=r[6]
        )
        for r in rows
    ]


def queue_rows_on(conn: Connection, initiative: str | None = None, kind: str | None = None) -> list[QueueRow]:
    return [cast(QueueRow, row) for row in store_queue.read(conn, initiative, kind)]


def work_items_stamp_on(conn: Connection, filters: Sequence[tuple[str, str | None]]) -> TableStamp:
    where, params = _where(conn, filters)
    # updated_at first, then every column, so a column a later migration adds is covered without naming it here.
    rows = conn.query_all(f"SELECT updated_at, * FROM work_items{where} ORDER BY initiative, task_id", params)
    return stamp_of(rows, 0)


def task_record_on(conn: Connection, run_id: str, phase: str, task: str) -> JsonObject | None:
    p = conn.dialect.placeholder
    sql = f"SELECT record_json FROM task_records WHERE run_id = {p} AND phase_id = {p} AND task_id = {p}"
    row = conn.query_one(sql, (run_id, phase, task))
    record = None if row is None else json_load(row[0])
    return record if isinstance(record, dict) else None


def task_record_phases_on(conn: Connection, run_id: str, task: str) -> list[str]:
    p = conn.dialect.placeholder
    sql = f"SELECT DISTINCT phase_id FROM task_records WHERE run_id = {p} AND task_id = {p} ORDER BY phase_id"
    return [str(r[0]) for r in conn.query_all(sql, (run_id, task))]


def run_task_ids_on(conn: Connection, run_id: str) -> list[str]:
    p = conn.dialect.placeholder
    sql = f"SELECT DISTINCT task_id FROM task_records WHERE run_id = {p} ORDER BY task_id"
    return [str(r[0]) for r in conn.query_all(sql, (run_id,))]


def resolve_on(conn: Connection, token: str) -> Resolved | None:
    # Imported here: `harness.store_ids` imports `harness.store_cli`, which imports this module.
    from harness.store_ids import resolve

    found = resolve(conn, token)
    return None if found is None else Resolved(*found)


def initiative_short_ids_on(conn: Connection) -> dict[str, str]:
    """Initiative to its short id: the id its intake row carries (`store_ids` numbers it `I<n>`)."""
    from harness.store_ids import INTAKE_TASK  # here for the same cycle as `resolve_on`

    p = conn.dialect.placeholder
    sql = f"SELECT initiative, short_id FROM work_items WHERE task_id = {p} AND short_id IS NOT NULL"
    return {str(r[0]): str(r[1]) for r in conn.query_all(sql, (INTAKE_TASK,))}


def queue_upsert_on(conn: Connection, row: JsonObject) -> Upserted | Failed:
    """A bad priority or issue number is Failed with the reason, and nothing is written."""
    try:
        return Upserted(store_queue.upsert(conn, row))
    except ValueError as exc:
        return Failed(EXIT_STORE_ERROR, str(exc))


def queue_claim_on(conn: Connection, initiative: str, task: str, holder: str, ttl_s: int, now: str) -> Claimed | Held:
    claimed = store_queue.claim(conn, initiative, task, holder, ttl_s, now)
    if claimed is None:
        return Held()
    return Claimed(Claim(holder=str(claimed["holder"]), epoch=int(claimed["epoch"]), expires_at=str(claimed["expires_at"])))


def queue_release_on(conn: Connection, initiative: str, task: str, holder: str) -> Released:
    return Released(store_queue.release(conn, initiative, task, holder))


# The export's own table, apart from the harness-owned work_items: one row per (initiative, task_id) holding the sha256
# of the file text `cox route export` last wrote. `IF NOT EXISTS` makes it additive and safe to rerun.
_EXPORT_HASHES_DDL = (
    "CREATE TABLE IF NOT EXISTS export_hashes "
    "(initiative TEXT NOT NULL, task_id TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY (initiative, task_id))"
)


def export_hashes_on(conn: Connection) -> dict[tuple[str, str], str]:
    conn.execute(_EXPORT_HASHES_DDL)
    return {(str(r[0]), str(r[1])): str(r[2]) for r in conn.query_all("SELECT initiative, task_id, sha256 FROM export_hashes")}


def set_export_hash_on(conn: Connection, initiative: str, task: str, digest: str) -> None:
    conn.execute(_EXPORT_HASHES_DDL)
    p = conn.dialect.placeholder
    conn.execute(
        f"INSERT INTO export_hashes (initiative, task_id, sha256) VALUES ({p}, {p}, {p}) "
        "ON CONFLICT (initiative, task_id) DO UPDATE SET sha256 = excluded.sha256",
        (initiative, task, digest),
    )


# --- by URL: open, call, close ---------------------------------------------------------------------------------------


def _with_store[T](
    url: str, opener: Callable[[str], Connection], act: Callable[[Connection], T]
) -> T | Failed | NotAvailable:
    """Edge. `act` on a connection `opener` gives for `url`; an open error is NotAvailable, a statement error Failed.

    Any other error `act` raises (a row that will not encode, a stored value that will not decode) is Failed too, with
    its type and message: an in-process caller such as the chair's tick gets a value it can report, never a crash.
    A statement that outlives `store_dialect.STATEMENT_TIMEOUT_MS` or a lock wait past `LOCK_TIMEOUT_MS` is a store
    error like any other, so every call here returns within those bounds."""
    try:
        conn = opener(url)
    except open_errors() as exc:
        return NotAvailable(f"cannot open the store: {type(exc).__name__}: {exc}")
    try:
        return act(conn)
    except db_errors() as exc:
        return Failed(EXIT_STORE_ERROR, f"cannot read the store: {type(exc).__name__}: {exc}")
    except Exception as exc:
        return Failed(EXIT_STORE_ERROR, f"the store call failed: {type(exc).__name__}: {exc}")
    finally:
        conn.close()


def _write[T](url: str, now: str, act: Callable[[Connection], T]) -> T | Failed | NotAvailable:
    return _with_store(url, lambda u: open_store(u, now), act)


def _read[T](url: str, act: Callable[[Connection], T]) -> T | Failed | NotAvailable:
    return _with_store(url, connect_readonly, act)


def set_work_state(
    url: str, initiative: str, task: str, state: WorkState, by: str, now: str,
    *, phase: str | None = None, expected: WorkState | None = None,
) -> SetStateResult:  # fmt: skip
    """Edge. `set_state_on` against the store at `url`. A blank `by` is refused before the store is opened, as the
    CLI's `--by` is: every state write names who made it."""
    if not by.strip():
        return Failed(EXIT_STORE_ERROR, "who made the change (by) must not be empty")
    problem = state_problem(state, expected)
    if problem is not None:
        return Failed(EXIT_STORE_ERROR, problem)
    return _write(url, now, lambda c: set_state_on(c, initiative, task, state, by, now, phase=phase, expected=expected))


def mark_task_landed(url: str, run_id: str, phase: str, task: str, pr: str, at: str, now: str) -> MarkLandedResult:
    return _write(url, now, lambda c: mark_landed_on(c, run_id, phase, task, pr, at))


def record_task_merge(url: str, initiative: str, task: str, commit: str, by: str, now: str) -> MergeResult:
    return _write(url, now, lambda c: record_merge_on(c, initiative, task, commit, by, now))


def record_phase_done(url: str, run_id: str, phase: str, branch: str, now: str) -> MergeResult:
    return _write(url, now, lambda c: record_phase_done_on(c, run_id, phase, branch))


def _lease(outcome: LeaseGranted | LeaseRefused | LeaseReleased | Failed | NotAvailable) -> LeaseResult:
    return LeaseError(outcome.detail) if isinstance(outcome, Failed) else outcome


def acquire_lease(url: str, name: str, holder: str, ttl: int, now: str, *, steal: bool = False) -> LeaseResult:
    return _lease(_write(url, now, lambda c: lease_acquire_on(c, name, holder, now, ttl, steal=steal)))


def renew_lease(url: str, name: str, holder: str, epoch: int, ttl: int, now: str) -> LeaseResult:
    return _lease(_write(url, now, lambda c: lease_renew_on(c, name, holder, epoch, now, ttl)))


def release_lease(url: str, name: str, holder: str, epoch: int, now: str) -> LeaseResult:
    return _lease(_write(url, now, lambda c: lease_release_on(c, name, holder, epoch)))


def pause_run(url: str, run_id: str, now: str, reason: str | None = None) -> PauseResult:
    return _write(url, now, lambda c: pause_on(c, run_id, now, reason))


def resume_run(url: str, run_id: str, now: str) -> ResumeResult:
    return _write(url, now, lambda c: resume_on(c, run_id))


def work_item_rows(url: str, initiative: str | None = None) -> list[WorkItemRow] | Failed | NotAvailable:
    return _read(url, lambda c: work_item_rows_on(c, initiative))


def queue_rows(url: str, initiative: str | None = None, kind: str | None = None) -> list[QueueRow] | Failed | NotAvailable:
    return _read(url, lambda c: queue_rows_on(c, initiative, kind))


def work_items_stamp(url: str, filters: Sequence[tuple[str, str | None]]) -> TableStamp | Failed | NotAvailable:
    return _read(url, lambda c: work_items_stamp_on(c, filters))


def task_record(url: str, run_id: str, phase: str, task: str) -> JsonObject | Failed | NotAvailable | None:
    return _read(url, lambda c: task_record_on(c, run_id, phase, task))


def task_record_phases(url: str, run_id: str, task: str) -> list[str] | Failed | NotAvailable:
    return _read(url, lambda c: task_record_phases_on(c, run_id, task))


def run_task_ids(url: str, run_id: str) -> list[str] | Failed | NotAvailable:
    return _read(url, lambda c: run_task_ids_on(c, run_id))


def resolve_token(url: str, token: str) -> Resolved | Failed | NotAvailable | None:
    return _read(url, lambda c: resolve_on(c, token))


def initiative_short_ids(url: str) -> dict[str, str] | Failed | NotAvailable:
    return _read(url, initiative_short_ids_on)


def upsert_queue_row(url: str, row: JsonObject, now: str) -> Upserted | Failed | NotAvailable:
    return _write(url, now, lambda c: queue_upsert_on(c, row))


def claim_queue_row(url: str, initiative: str, task: str, holder: str, ttl_s: int, now: str) -> Claimed | Held | Failed | NotAvailable:
    return _write(url, now, lambda c: queue_claim_on(c, initiative, task, holder, ttl_s, now))


def release_queue_row(url: str, initiative: str, task: str, holder: str, now: str) -> Released | Failed | NotAvailable:
    return _write(url, now, lambda c: queue_release_on(c, initiative, task, holder))


def export_hashes(url: str, now: str) -> dict[tuple[str, str], str] | Failed | NotAvailable:
    return _write(url, now, export_hashes_on)


def set_export_hash(url: str, initiative: str, task: str, digest: str, now: str) -> Failed | NotAvailable | None:
    return _write(url, now, lambda c: set_export_hash_on(c, initiative, task, digest))
