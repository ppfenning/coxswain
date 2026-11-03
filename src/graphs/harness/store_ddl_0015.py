"""Migration 0015: work_items.merged_commit and work_items.handoff_json.

merged_commit is a nullable TEXT column holding the commit a task's branch was merged at, and NULL
means not merged. handoff_json is a nullable TEXT column holding the task's handoff record as JSON.
No default and no constraint, so existing rows keep NULL. No index, no foreign-key clause,
matching every migration since 0002.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 15
DESCRIPTION = "work_items gains merged_commit and handoff_json"

Column = tuple[str, str, str]  # table, column, type

# Table, column, type for each ALTER, in the order applied.
_COLUMNS: tuple[Column, ...] = (
    ("work_items", "merged_commit", "TEXT"),
    ("work_items", "handoff_json", "TEXT"),
)

# Table and column that the ALTERs append; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5/ddl9/ddl10.
_ADDED: tuple[tuple[str, str], ...] = tuple((t, c) for t, c, _ in _COLUMNS)


def _alter(table: str, column: str, kind: str) -> str:
    return f"ALTER TABLE {table} ADD COLUMN {column} {kind}"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """One ALTER per added column, the same on both dialects: the columns are plain TEXT."""
    return tuple(_alter(t, c, k) for t, c, k in _COLUMNS)
