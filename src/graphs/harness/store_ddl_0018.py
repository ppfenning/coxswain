"""Migration 0018: cartridge_snapshots.

The cartridges a chair launches with, published to the store so a lane host reads them from there and no machine
copies them to another. One row is one whole cartridges directory: `sha` is the digest of its files (path, mode and
bytes), `files_json` holds them, and `at` and `by` say when and which machine published it. A row is never updated:
the same directory gives the same sha, and a changed one gives a new row.

Every column is NOT NULL. No index, no foreign-key clause, matching every migration since 0002.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 18
DESCRIPTION = "cartridge_snapshots table"


Column = tuple[str, str]

# Name, columns, key.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    ("cartridge_snapshots", (("sha", "TEXT"), ("at", "TEXT"), ("by", "TEXT"), ("files_json", "TEXT")), ("sha",)),
)


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The one CREATE TABLE, the same on both dialects."""
    name, columns, key = _TABLES[0]
    defs = ",\n".join(f"    {c} {t} NOT NULL" for c, t in columns)
    return (f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)",)
