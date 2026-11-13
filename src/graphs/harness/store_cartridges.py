"""Cartridge snapshots in the store: put one under its sha, get it back. Nothing here reads a file or a cartridge."""

from __future__ import annotations

from harness.store_dialect import Connection, insert_ignore

_COLUMNS = ("sha", "at", "by", "files_json")


def put(conn: Connection, sha: str, at: str, by: str, files_json: str) -> bool:
    """Store a snapshot. False when this sha is already there: the same files are never stored twice."""
    return conn.execute(insert_ignore(conn.dialect, "cartridge_snapshots", _COLUMNS, ("sha",)), (sha, at, by, files_json)) == 1


def get(conn: Connection, sha: str) -> str | None:
    """The snapshot's files as stored, None when no snapshot has this sha."""
    p = conn.dialect.placeholder
    row = conn.query_one(f"SELECT files_json FROM cartridge_snapshots WHERE sha = {p}", (sha,))
    return None if row is None else str(row[0])


def has(conn: Connection, sha: str) -> bool:
    p = conn.dialect.placeholder
    return conn.query_one(f"SELECT 1 FROM cartridge_snapshots WHERE sha = {p}", (sha,)) is not None
