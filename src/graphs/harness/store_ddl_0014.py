"""Migration 0014: leases.progress.

leases.progress is a nullable TEXT column carrying what a lease holder's lane has done so far,
written by renew alongside heartbeat_at, so a remote lane's progress travels through the shared
store. No default and no constraint, so existing rows keep NULL. No index, no foreign-key clause,
matching every migration since 0002.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 14
DESCRIPTION = "leases gains progress"

Column = tuple[str, str, str]  # table, column, type

# Table, column, type for each ALTER, in the order applied.
_COLUMNS: tuple[Column, ...] = (("leases", "progress", "TEXT"),)

# Table and column that the ALTERs append; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5/ddl9/ddl10.
_ADDED: tuple[tuple[str, str], ...] = tuple((t, c) for t, c, _ in _COLUMNS)


def _alter(table: str, column: str, kind: str) -> str:
    return f"ALTER TABLE {table} ADD COLUMN {column} {kind}"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """One ALTER per added column, the same on both dialects: the column is plain TEXT."""
    return tuple(_alter(t, c, k) for t, c, k in _COLUMNS)
