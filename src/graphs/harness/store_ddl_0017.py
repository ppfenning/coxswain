"""Migration 0017: courier_log.

The courier bus as an append-only log in the store, one row per line, so every machine reads and writes one bus.
A line is the JSON text of one bus entry, exactly as a line of the `courier.jsonl` file it replaces. An ack is one
more line for the same entry id, never an update: reading folds the lines by id, and that fold does not depend on
the order two machines' lines arrive in. `line_id` is unique per line; `at` orders the log.

Every column is NOT NULL. No index, no foreign-key clause, matching every migration since 0002.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 17
DESCRIPTION = "courier_log table"


Column = tuple[str, str]

# Name, columns, key.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    ("courier_log", (("line_id", "TEXT"), ("at", "TEXT"), ("line", "TEXT")), ("line_id",)),
)


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The one CREATE TABLE, the same on both dialects."""
    name, columns, key = _TABLES[0]
    defs = ",\n".join(f"    {c} {t} NOT NULL" for c, t in columns)
    return (f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)",)
