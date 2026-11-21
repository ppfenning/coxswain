"""Migration 0023: `workers` gains `machine_id`, the machine that wrote the row.

Two machines left on one default host name would share one worker name, and with it one `workers` row and one
`claimed_by`. The row now says which machine wrote it, and `store_phases.register` refuses another machine's live
row of the same name (`worker_identity.takeover`).

Additive (`ADDITIVE = True`): one column with a default, so a build one migration older keeps opening, reading and
writing a store at this version (`store_migrate.accepts`). Its upsert names only the columns it knows, so it leaves
`machine_id` as it is, and a row it inserts gets ''. An older build writes `beat_at` only when it registers, so the
age of a '' row's beat says nothing about whether its worker still runs: `worker_identity.takeover` therefore never
takes over a '' row while it says `up`, whatever its beat. Only the older worker's own clean stop (`stopping`) frees
the name. Upgrade every machine that runs a worker of one name before starting any of them on the new build.

On Postgres, 0021's statement trigger on `workers` fires on an UPDATE OF every column but the beat (`beat_at`), as
the columns stood then. It is replaced, in place and under the same name, by one whose UPDATE OF list also names
`machine_id` (`WATCHED`), so the new column wakes listeners as every other non-beat column does. A worker's own beat
(`store_phases.beat_worker`) still sets `beat_at` alone, and wakes nobody.
"""

from __future__ import annotations

from harness.store_ddl_0021 import WATCHED as WATCHED_0021
from harness.store_ddl_0021 import notified_columns
from harness.store_dialect import Dialect

VERSION = 23
DESCRIPTION = "workers gains machine_id"
ADDITIVE = True
ADDITIVE_EXEMPT = {
    "trigger": (
        "The one CREATE OR REPLACE TRIGGER replaces 0021's cox_notify trigger on workers with the same AFTER ... FOR "
        "EACH STATEMENT trigger calling the same function, its UPDATE OF list one column longer (machine_id). As "
        "0021's, it fires once the statement's rows are written and Postgres ignores what it returns, so it cannot "
        "skip, drop or rewrite an older writer's row; an older writer's statements fire it exactly as before."
    ),
}

# Table, column, type, the column's tail after its type.
_COLUMNS: tuple[tuple[str, str, str, str], ...] = (("workers", "machine_id", "TEXT", " NOT NULL DEFAULT ''"),)

# Table and column that the ALTERs append; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5/ddl9/ddl20.
ADDED: tuple[tuple[str, str], ...] = tuple((t, c) for t, c, _, _ in _COLUMNS)

# The watched tables whose trigger this migration replaces, with their columns from here on (0021's `WATCHED` shape).
WATCHED: tuple[tuple[str, tuple[str, ...]], ...] = (("workers", (*dict(WATCHED_0021)["workers"], "machine_id")),)


def _trigger(table: str, columns: tuple[str, ...]) -> str:
    return (f"CREATE OR REPLACE TRIGGER cox_notify AFTER INSERT OR DELETE OR TRUNCATE OR "
            f"UPDATE OF {', '.join(notified_columns(table, columns))} ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION cox_notify()")


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The one ALTER on both dialects; on Postgres, then, the workers trigger with machine_id in its UPDATE OF list."""
    alters = tuple(f"ALTER TABLE {t} ADD COLUMN {c} {k}{tail}" for t, c, k, tail in _COLUMNS)
    if dialect.name != "postgres":
        return alters
    return (*alters, *(_trigger(t, cs) for t, cs in WATCHED))
