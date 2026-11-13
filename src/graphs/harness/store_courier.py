"""The courier bus in the store: append one line, read every line in order. Nothing here parses a line."""

from __future__ import annotations

from collections.abc import Sequence

from harness.store_dialect import Connection, insert_ignore

_COLUMNS = ("line_id", "at", "line")


def append(conn: Connection, line_id: str, at: str, line: str) -> bool:
    """Add one line. False when a line with this id is already there, so an import or a retry never doubles one."""
    return conn.execute(insert_ignore(conn.dialect, "courier_log", _COLUMNS, ("line_id",)), (line_id, at, line)) == 1


def append_many(conn: Connection, rows: Sequence[tuple[str, str, str]]) -> int:
    """Add several `(line_id, at, line)` rows in one transaction; the number added."""
    with conn.transaction():
        return sum(append(conn, *row) for row in rows)


def lines(conn: Connection) -> list[str]:
    """Every line, oldest first; lines written at the same instant keep a stable order."""
    return [row[0] for row in conn.query_all("SELECT line FROM courier_log ORDER BY at, line_id")]


def count(conn: Connection) -> int:
    row = conn.query_one("SELECT count(*) FROM courier_log")
    return int(row[0]) if row else 0
