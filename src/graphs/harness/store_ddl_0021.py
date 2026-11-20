"""Migration 0021: chair_queue, store_cursor, and on Postgres the trigger that bumps the cursor.

`chair_queue` is the event-driven chair's own queue, one item per (kind, key), shaped on `land_queue`; nothing reads
or writes it until the queue workers land (C3). `store_cursor` holds one row per watched table, plus the gate row
`'*'`, and each row's `seq` rises inside the transaction of every statement that writes its table. The value is a wake
and a cache key, never the record: a reader that sees a table's `seq` move re-reads that table's rows as they now are.
It has the shape of the change counters the chair's tick cache already takes (`chair_tick_cache.TableCounters`), and
unlike those statistics it commits with the write, so it never trails one.

On Postgres one function, `cox_stamp`, runs as a `BEFORE ... FOR EACH STATEMENT` trigger on each watched table. It
bumps the gate row first, then the table's own row. Every writer of a watched table therefore takes the gate before it
locks any of its rows, so two transactions that write two watched tables in opposite orders queue on the gate instead
of deadlocking on each other's rows. The function is `SECURITY DEFINER`, so it runs as the store owner who ran this
migration: the worker's scoped role (`store_roles`) may only read `store_cursor`, and without it every worker write
would fail with 42501. Its `search_path` is pinned to `pg_catalog, pg_temp`, so no schema a worker can create in is
ever searched while it runs as the owner. The table it updates is named through `TG_TABLE_SCHEMA`, the firing table's
own schema, so a store in a schema of its own (each test's, or `cox` beside `public`) bumps its own cursor and no other.

A heartbeat does not bump the cursor. On a table whose heartbeat is an UPDATE of a few columns (`BEATS`), the trigger
fires only for an UPDATE that sets some other column; an INSERT or DELETE always fires it. The column list is this
version's: a column a later migration adds stamps only when it is set beside one listed here.

sqlite, the suites' double, gets both tables and their seed rows but no trigger: its rows stay at 0. The chair's cache
never reads a sqlite store's counters (`run_store.table_counters` returns None there, which caches nothing), and
`store_cursor.counters` reads None for sqlite too.

Additive (`ADDITIVE = True`): two new tables, one index on one of them, the cursor's seed rows, one function and a
trigger per watched table; nothing an older build reads or writes changes shape. `ADDITIVE_EXEMPT` says why the
trigger and its function cannot drop or rewrite an older writer's row.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 21
DESCRIPTION = "chair_queue and store_cursor tables; a statement trigger stamps the cursor on each watched table"
ADDITIVE = True
ADDITIVE_EXEMPT = {
    "trigger": (
        "Each CREATE TRIGGER is a FOR EACH STATEMENT trigger. Postgres ignores the value a statement-level trigger "
        "returns, BEFORE or AFTER, so it cannot skip, drop or rewrite any row the statement writes; the function only "
        "updates store_cursor, a table this migration creates and no older build writes. Its one way to touch the "
        "write is to fail it: store_cursor missing, which this same migration rules out, or a wait on the gate row "
        "longer than the store's lock bound, which is the old write's own bound on any row it locks."
    ),
    "function": (
        "cox_stamp has no arguments and returns NULL, which a statement-level trigger's caller ignores. Its body runs "
        "one UPDATE twice, on store_cursor's gate row '*' and then on its row named TG_TABLE_NAME, setting seq + 1 "
        "and at to the statement's time, in the firing table's own schema (quote_ident(TG_TABLE_SCHEMA)). It reads "
        "and writes no other table. SECURITY DEFINER lets a worker's scoped role, which may only read store_cursor, "
        "still bump it; search_path is pinned to pg_catalog, pg_temp so that grant reaches nothing else."
    ),
}

Column = tuple[str, str]

# Name, columns, key: the tables `store_copy` copies. store_cursor is not one: each store's triggers keep its own, and
# a copied seq could move the destination's cursor backwards onto a value a reader already cached under.
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

CURSOR_TABLE: tuple[str, tuple[Column, ...], tuple[str, ...]] = (
    "store_cursor",
    (("name", "TEXT"), ("seq", "BIGINT"), ("at", "TEXT")),
    ("name",),
)

GATE = "*"

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

# The columns each heartbeat sets, and nothing else: a phase claim's (`store_phases`, §1.2 of the stateless plan), a
# land claim's renewal (`landing_store.renew`), a host's beat (`store_hosts.host_beat`) and a worker's beat.
BEATS: dict[str, tuple[str, ...]] = {
    "phase_queue": ("heartbeat_at", "lease_until"),
    "land_queue": ("lease_until", "updated_at"),
    "hosts": ("beat_at", "versions_json"),
    "workers": ("beat_at",),
}

_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_chair_queue_due", "chair_queue", ("kind", "state", "due_at")),
)

# Dollar-quoted with a tag of its own, so no `$$` inside can end it. Each UPDATE names one row, the gate first: a
# single `name IN (...)` would lock the two rows in whatever order the scan finds them. No `%` anywhere: psycopg reads
# one as a placeholder, since `migrate` passes every statement an (empty) parameter tuple.
_FUNCTION = """CREATE OR REPLACE FUNCTION cox_stamp() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $cox_stamp$
DECLARE
    stamped text := to_char(statement_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
    bump text := 'UPDATE ' || quote_ident(TG_TABLE_SCHEMA)
        || '.store_cursor SET seq = seq + 1, at = $1 WHERE name = $2';
BEGIN
    EXECUTE bump USING stamped, '*';
    EXECUTE bump USING stamped, TG_TABLE_NAME;
    RETURN NULL;
END
$cox_stamp$"""


def stamped_columns(table: str, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Pure. The columns whose UPDATE stamps `table`: every one but its heartbeat's, in DDL order."""
    beat = BEATS.get(table, ())
    return tuple(c for c in columns if c not in beat)


def _trigger(table: str, columns: tuple[str, ...]) -> str:
    update = f"UPDATE OF {', '.join(stamped_columns(table, columns))}" if table in BEATS else "UPDATE"
    return (f"CREATE TRIGGER cox_stamp BEFORE INSERT OR DELETE OR {update} ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION cox_stamp()")


def _table(name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {t} NOT NULL" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The two CREATE TABLEs, the queue's index and the cursor's seed rows on both dialects; then, on Postgres only,
    the stamp function and one trigger per watched table."""
    rows = ", ".join(f"('{name}', 0, '')" for name in (GATE, *(t for t, _ in WATCHED)))
    common = (
        *(_table(n, c, k) for n, c, k in (*TABLES, CURSOR_TABLE)),
        *(f"CREATE INDEX IF NOT EXISTS {i} ON {t} ({', '.join(cs)})" for i, t, cs in _INDEXES),
        f"INSERT INTO store_cursor (name, seq, at) VALUES {rows} ON CONFLICT (name) DO NOTHING",
    )
    if dialect.name != "postgres":
        return common
    return (*common, _FUNCTION, *(_trigger(t, cs) for t, cs in WATCHED))
