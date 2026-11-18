"""Migration 0019: lander_notices.

What the lander has told the chair about a stall it cannot record on a landing record: one row per stall, so the
chair is told once, on every machine and across restarts. `key` names the stall (`unreadable:<initiative>`: a
landing-record row that does not read as a record, a row that must never be written over), `reason` is the told
reason with its variable parts replaced (`lander.reason_key`), and `since` is when it was first told. The row is
deleted when the stall clears, so the next stall is told again.

Every column is NOT NULL. The primary key on `key` is the table's only index; no foreign-key clause, matching every
migration since 0002.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 19
DESCRIPTION = "lander_notices table"


Column = tuple[str, str]

# Name, columns, key.
TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    ("lander_notices", (("key", "TEXT"), ("reason", "TEXT"), ("since", "TEXT")), ("key",)),
)


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The one CREATE TABLE, the same on both dialects."""
    name, columns, key = TABLES[0]
    defs = ",\n".join(f"    {c} {t} NOT NULL" for c, t in columns)
    return (f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)",)
