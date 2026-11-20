"""The store's change cursor (`harness.store_ddl_0021`), as the chair's tick cache reads it. Pure; no I/O.

A Postgres store at schema 21 or later keeps `store_cursor`: one `seq` per watched table, bumped by a statement
trigger inside the transaction of every write to that table, heartbeats excepted. Where the statistics counters
(`pg_stat_user_tables`) trail a write by up to a minute, the cursor moves when the write commits, and not before.

`run_store.table_counters` reads both in one connection: the statistics first, which also say whether the cursor
table is visible on the connection's `search_path`, then the cursor when it is. `merged` lays the cursor's values over
the statistics, so a reader of a watched table is keyed by its exact `seq` and a reader of any other table keeps the
statistics counter it had. Reading the cursor before the rows it guards is what makes a cached value safe: a write
that commits between the two reads moves the cursor past what was read, so the next tick reads again.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["READ_SQL", "TABLE", "merged", "seqs"]

TABLE = "store_cursor"
READ_SQL = f"SELECT name, seq FROM {TABLE}"


def seqs(rows: Sequence[Any]) -> dict[str, int]:
    """Pure. `READ_SQL`'s rows as {name: seq}; takes mapping rows (psycopg's dict_row) and plain pairs alike."""
    pairs = ((r["name"], r["seq"]) if isinstance(r, Mapping) else (r[0], r[1]) for r in rows)
    return {str(name): int(seq) for name, seq in pairs}


def merged(statistics: Mapping[str, int], cursor: Mapping[str, int] | None) -> dict[str, int]:
    """Pure. The statistics counters with each table the cursor watches replaced by its `seq`; the statistics alone
    when the store has no cursor (None). The cursor's own gate row (`*`) comes along; no reader names it."""
    return {**statistics, **(cursor or {})}
