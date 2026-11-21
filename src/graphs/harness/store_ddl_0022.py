"""Migration 0022: indexes for the store's hot time and kind predicates, on node_calls and chair_actions.

`node_calls` had no index on `ts`, so every `run_store.cost_since` (`SUM(cost_usd) ... WHERE ts >= ?`), which the
chair calls for its budget and docket gates on every tick and the dash feed on every build, scanned the whole table;
`ix_node_calls_ts` turns each into a range read. `chair_actions` is keyed on (ts, holder, kind, target), so its
`ts >= ?` readers already range-read the key, but nothing served `kind = ?`: the newest `meter` and `status` row,
the `housekeeping`, tuning and `stalled_usr1` rows, and the kept kinds of the chair's action window each scanned the
table. `ix_chair_actions_kind_ts` serves those, and its `ts` column answers their `ORDER BY ts DESC LIMIT` too.

Additive (`ADDITIVE = True`): two non-unique indexes on existing tables, created `IF NOT EXISTS`, so a build one
migration older keeps opening, reading and writing a store at this version (`store_migrate.accepts`).

Lock choice: a plain `CREATE INDEX`, not `CONCURRENTLY`. `migrate` runs each migration's statements inside one
transaction (`conn.transaction()`, with its `schema_version` row and the `SHARE ROW EXCLUSIVE` lock on that table),
and Postgres refuses `CREATE INDEX CONCURRENTLY` inside a transaction block, so using it would mean a second,
non-transactional runner path for one migration. A plain build takes a SHARE lock on its table, which blocks that
table's writers (not its readers) until the transaction commits. On Postgres 16 with 60,000 node_calls and 30,000
chair_actions rows the whole migration, both builds included, took 0.56 s.

The wait for that lock is not short by construction. A build waits for its SHARE lock for as long as
`store_migrate.MIGRATION_LOCK_TIMEOUT_MS` (300 s) allows, and while it waits, every later writer to that table queues
behind it. Both builds run in one transaction, so the lock on node_calls is held while the chair_actions build waits
for its own. Today the store's writes are short row writes, so the wait drains almost at once and the stall is
negligible. A long-running writer, or a table grown large enough that a build takes long, needs a concurrent path
built first, not this one.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 22
DESCRIPTION = "indexes on node_calls (ts) and chair_actions (kind, ts)"
ADDITIVE = True

# Index name, table, columns.
INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_node_calls_ts", "node_calls", ("ts",)),
    ("ix_chair_actions_kind_ts", "chair_actions", ("kind", "ts")),
)


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The two CREATE INDEX IF NOT EXISTS statements; the same on both dialects."""
    return tuple(f"CREATE INDEX IF NOT EXISTS {i} ON {t} ({', '.join(cs)})" for i, t, cs in INDEXES)
