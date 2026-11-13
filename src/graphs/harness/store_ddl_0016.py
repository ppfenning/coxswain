"""Migration 0016: landing_records and land_queue.

landing_records holds one row per initiative: its landing record (the initiative branch, the resolved parent, the
base sha and each phase's state, tip and merge sha) as JSON, with the initiative state beside it so a status read
needs no parse. land_queue is the durable land queue: one row per item, `phase_merge` or `initiative_land`, keyed by
kind, initiative and phase. A claimed row names its worker and when the claim lapses; an item whose worker died is
taken again once `lease_until` has passed.

Every column is NOT NULL, with the empty string or 0 for "none". No index, no foreign-key clause, matching every
migration since 0002.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 16
DESCRIPTION = "landing_records and land_queue tables"

Column = tuple[str, str]

# Name, columns, key.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "landing_records",
        (
            ("initiative", "TEXT"),
            ("repo", "TEXT"),
            ("state", "TEXT"),
            ("record_json", "TEXT"),
            ("created_at", "TEXT"),
            ("updated_at", "TEXT"),
        ),
        ("initiative",),
    ),
    (
        "land_queue",
        (
            ("id", "TEXT"),
            ("kind", "TEXT"),
            ("initiative", "TEXT"),
            ("phase", "TEXT"),
            ("repo", "TEXT"),
            ("parent", "TEXT"),
            ("state", "TEXT"),
            ("reason", "TEXT"),
            ("queued_at", "TEXT"),
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


def _table(name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {t} NOT NULL" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The two CREATE TABLEs, the same on both dialects."""
    return tuple(_table(n, c, k) for n, c, k in _TABLES)
