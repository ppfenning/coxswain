"""Migration 0021: chair_queue, and on Postgres a statement trigger that sends a wake on each watched table's write.

`chair_queue` is the event-driven chair's own queue, one item per (kind, key), shaped on `land_queue`. Nothing reads
or writes it until the queue workers land (C3).

On Postgres one function, `cox_notify`, runs as an `AFTER ... FOR EACH STATEMENT` trigger on each watched table, and
does one thing: `pg_notify('cox_store', <table>)`. A notification is transactional. It is delivered to the listeners
(`harness.store_notify`) when the writer commits, once per distinct payload in that transaction, and dropped if the
writer rolls back. It is a wake and nothing more: a listener re-reads the rows, and polling stays the fallback, so a
missed notification costs latency, never correctness. The chair's tick cache keeps the statistics counters as its key
(`run_store.table_counters`); this migration does not change that.

Why this is safe for older writers, and why no table and no row is involved:
- The function takes no row lock and writes no row, so it cannot deadlock a writer, hold a hot row, or make a writer
  wait on another session's rows. Postgres serialises notifying commits on a short queue lock, taken at commit after
  every lock the writer already holds.
- The one way it can fail an older writer is a full notification queue. A session that LISTENs and never drains
  (or sits in an open transaction) holds the queue's tail back; once the queue is full, every writer to a watched
  table fails at commit with 54000 "too many notifications in the NOTIFY queue". The default queue is 8 GB and a
  payload is a table name, which makes this remote, not impossible. The listener (`store_notify`) runs in autocommit
  and never opens a transaction; C3's listener gets a doctor check on `pg_notification_queue_usage()`.
- It runs with the invoker's rights (no SECURITY DEFINER). NOTIFY needs no privilege, so a worker's scoped role
  (`store_roles`) writes and notifies with no new grant. Postgres checks EXECUTE on a trigger function only when a
  trigger is created, not when it fires.
- `REVOKE EXECUTE ... FROM PUBLIC` leaves the function to its owner, so no other role can attach it to a table of
  its own. Even attached, it could only send a notification, which any role may send anyway.
- Its `search_path` is pinned to `pg_catalog, pg_temp`, and it calls `pg_catalog.pg_notify` by its full name.

A heartbeat does not wake anyone. On a table whose heartbeat is an UPDATE of a few columns (`BEATS`), the trigger fires
only for an UPDATE that sets some other column. An INSERT, DELETE or TRUNCATE always fires it. `hosts` has no beat
exemption: `store_hosts.host_beat` always sets `versions_json`, which is data (the login check, repos, the workspace).
The column list is this version's: a column a later migration adds fires the trigger only when it is set alongside
one listed here.

sqlite, the suites' double, gets the table and no trigger. It has no NOTIFY.

Additive (`ADDITIVE = True`): one new table and its index, one function, its revoke, and a trigger per watched table.
Nothing an older build reads or writes changes shape. `ADDITIVE_EXEMPT` says why the trigger and its function cannot
drop, rewrite or block an older writer's row.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 21
DESCRIPTION = "chair_queue table; a statement trigger on each watched table sends a cox_store notification"
ADDITIVE = True
ADDITIVE_EXEMPT = {
    "trigger": (
        "Each CREATE TRIGGER is an AFTER ... FOR EACH STATEMENT trigger. It fires once the statement's rows are "
        "written, and Postgres ignores what a statement-level trigger returns, so it cannot skip, drop or rewrite a "
        "row. Its function writes no row and takes no row lock: the only lock NOTIFY adds is Postgres's short queue "
        "lock at commit, taken after every lock the writer holds, so it cannot deadlock an older writer either. The "
        "one way it can fail an older writer is a full notify queue (a listener that never drains): every writer to "
        "a watched table then fails at commit with 54000. The default 8 GB queue makes that remote."
    ),
    "function": (
        "cox_notify has no arguments, returns NULL, and runs one statement: pg_catalog.pg_notify('cox_store', "
        "TG_TABLE_NAME). It reads and writes no table. It runs with the invoker's rights (no SECURITY DEFINER), its "
        "search_path is pinned, and EXECUTE is revoked from PUBLIC so no other role can attach it to a table."
    ),
}

CHANNEL = "cox_store"

Column = tuple[str, str]

# Name, columns, key: the tables `store_copy` copies.
TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "chair_queue",
        (
            ("id", "TEXT"),
            ("kind", "TEXT"),
            ("key", "TEXT"),
            ("state", "TEXT"),
            ("reason", "TEXT"),
            ("queued_at", "TEXT"),
            ("due_at", "TEXT"),
            ("claimed_by", "TEXT"),
            ("claimed_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("attempts", "INTEGER"),
            ("last_error", "TEXT"),
            ("updated_at", "TEXT"),
        ),
        ("id",),
    ),
)

# Each watched table and its columns at this version, in DDL order. The trigger on a table with a heartbeat names
# every column but the heartbeat's in its UPDATE OF list.
WATCHED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("work_items", (
        "initiative", "task_id", "phase", "state", "needs_json", "updated_at", "updated_by", "kind", "title",
        "surfaces_json", "body", "extra_json", "holder", "epoch", "expires_at", "short_id", "priority", "issue_number",
        "project_item_id", "parent_item", "merged_commit", "handoff_json",
    )),
    ("runs", (
        "run_id", "principal", "launched_by", "launched_at", "cartridge_sha", "cartridge_team", "overlay_sha",
        "provider_profile", "started_at", "ended_at", "status", "record_json", "graph_id", "host", "paused_at",
        "short_id",
    )),
    ("phase_queue", (
        "initiative", "phase", "kind", "repo", "base_ref", "branch", "requires_json", "state", "outcome", "reason",
        "band", "seq", "budget_usd", "queued_at", "ready_at", "claimed_by", "claim_epoch", "run_id", "claimed_at",
        "heartbeat_at", "lease_until", "attempts", "updated_at", "updated_by",
    )),
    ("step_results", (
        "run_id", "step", "initiative", "phase", "task_id", "kind", "round", "ok", "verdict", "subject_sha", "worker",
        "claim_epoch", "started_at", "finished_at", "cost_usd", "result_json", "artifacts_json",
    )),
    ("review_rounds", (
        "initiative", "phase", "round", "head_sha", "verdict", "reason", "revision_json", "evidence_step", "run_id",
        "worker", "claim_epoch", "cost_usd", "decided_at",
    )),
    ("land_queue", (
        "id", "kind", "initiative", "phase", "repo", "parent", "state", "reason", "queued_at", "claimed_by",
        "claimed_at", "lease_until", "attempts", "last_error", "updated_at",
    )),
    ("landing_records", ("initiative", "repo", "state", "record_json", "created_at", "updated_at")),
    ("hosts", (
        "name", "ssh", "capacity", "state", "beat_at", "versions_json", "updated_at", "updated_by", "threads",
        "capabilities_json",
    )),
    ("workers", (
        "name", "capabilities_json", "lanes", "threads", "state", "version", "doctor_json", "doctor_at", "started_at",
        "beat_at",
    )),
)

# The columns each heartbeat sets, and nothing else: a phase claim's (`store_phases`, §1.2 of the stateless plan),
# a land claim's renewal (`landing_store.renew`) and a worker's beat.
BEATS: dict[str, tuple[str, ...]] = {
    "phase_queue": ("heartbeat_at", "lease_until"),
    "land_queue": ("lease_until", "updated_at"),
    "workers": ("beat_at",),
}

_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_chair_queue_due", "chair_queue", ("kind", "state", "due_at")),
)

# No `%` anywhere: psycopg reads one as a placeholder, since `migrate` passes every statement an (empty)
# parameter tuple.
_FUNCTION = f"""CREATE OR REPLACE FUNCTION cox_notify() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $cox_notify$
BEGIN
    PERFORM pg_catalog.pg_notify('{CHANNEL}', TG_TABLE_NAME);
    RETURN NULL;
END
$cox_notify$"""

_REVOKE = "REVOKE EXECUTE ON FUNCTION cox_notify() FROM PUBLIC"


def notified_columns(table: str, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Pure. The columns whose UPDATE fires `table`'s trigger: every one but its heartbeat's, in DDL order."""
    beat = BEATS.get(table, ())
    return tuple(c for c in columns if c not in beat)


def _trigger(table: str, columns: tuple[str, ...]) -> str:
    update = f"UPDATE OF {', '.join(notified_columns(table, columns))}" if table in BEATS else "UPDATE"
    return (f"CREATE TRIGGER cox_notify AFTER INSERT OR DELETE OR TRUNCATE OR {update} ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION cox_notify()")


def _table(name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {t} NOT NULL" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The CREATE TABLE and its index on both dialects. On Postgres only, after them: the function, its revoke, and
    one trigger per watched table."""
    common = (
        *(_table(n, c, k) for n, c, k in TABLES),
        *(f"CREATE INDEX IF NOT EXISTS {i} ON {t} ({', '.join(cs)})" for i, t, cs in _INDEXES),
    )
    if dialect.name != "postgres":
        return common
    return (*common, _FUNCTION, _REVOKE, *(_trigger(t, cs) for t, cs in WATCHED))
