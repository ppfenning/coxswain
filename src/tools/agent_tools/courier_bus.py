"""Where the courier bus is kept: the store's `courier_log` when the workspace has a shared store, else the file.

Callers name the bus by its old path, `<workspace>/courier.jsonl`, and go through `courier.read_text`,
`courier.append_file` and `courier.ack_file`. Those ask this module where the lines live. With a Postgres store the
bus is the `courier_log` table, one bus for every machine; a workspace with only its own local store keeps the file,
which is all one machine needs.

The first write to the table loads the lines a `courier.jsonl` already holds, once: nothing sent before the move is
lost. A read never writes: its connection is read-only, never migrates and never imports (`opening`). While the log is
missing or empty, a read shows the file's lines, which are exactly what the first write will load (`readable`).
Migrations run on a write path or in `cox setup migrate`, never because something looked at the bus: `cox chair run
--dry-run` reads it on every tick.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

_CONNECTIONS: dict[str, Any] = {}  # migrated, for writes
_READERS: dict[str, Any] = {}  # read-only and never migrated, for reads
_IMPORTED: set[str] = set()

Purpose = Literal["read", "write"]

# The session setting that makes the server refuse any write on a reader's connection, by dialect name.
_READ_ONLY = {"postgres": "SET SESSION default_transaction_read_only = on", "sqlite": "PRAGMA query_only = ON"}


@dataclass(frozen=True)
class Opening:
    """How a connection for one purpose is opened: whether it migrates the schema, and whether its session is read-only."""

    migrate: bool
    read_only: bool


def opening(purpose: Purpose) -> Opening:
    """Pure. A write's connection migrates the schema first; a read's never does, and its session refuses writes."""
    if purpose == "write":
        return Opening(migrate=True, read_only=False)
    return Opening(migrate=False, read_only=True)


def readable(store_lines: Sequence[str] | None, file_lines: Sequence[str]) -> list[str]:
    """Pure. The lines a reader sees: the store's, or the file's while the log is missing (None) or empty, since those
    are what the first write will load into it."""
    return list(store_lines) if store_lines else list(file_lines)


def readable_count(logged: int | None, file_lines: int) -> int:
    """Pure. How many lines `readable` shows, from the counts: the store's when it holds any, else the file's."""
    return logged if logged else file_lines


def store_url(path: Path) -> str:
    """Edge. The store URL of the workspace whose bus is at `path`."""
    from agent_tools import run_store

    return run_store.store_url_for(Path(path).parent / "runs")


def uses_store(path: Path) -> bool:
    """Whether the bus at `path` lives in the store: only for a shared (Postgres) store."""
    try:
        return store_url(path).startswith(("postgresql", "postgres"))
    except Exception:
        return False


def _conn(path: Path) -> Any:
    """Edge. One migrated connection per store URL for the life of the process, for a write."""
    return _open(path, "write")


def _read_conn(path: Path) -> Any:
    """Edge. One read-only connection per store URL for the life of the process. It never migrates."""
    return _open(path, "read")


def _open(path: Path, purpose: Purpose) -> Any:
    from harness.store_dialect import connect
    from harness.store_migrate import default_modules, migrate

    how = opening(purpose)
    cache = _CONNECTIONS if purpose == "write" else _READERS
    url = store_url(path)
    if url not in cache:
        conn = connect(url)
        try:
            if how.read_only:
                conn.execute(_READ_ONLY[conn.dialect.name])
            if how.migrate:
                migrate(conn, _stamp(), default_modules())
        except BaseException:
            conn.close()
            raise
        cache[url] = conn
    return cache[url]


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _file_lines(path: Path) -> list[str]:
    """Edge, read only. The non-blank lines of a `courier.jsonl`; none when there is no file."""
    if not Path(path).is_file():
        return []
    return [ln for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]


def _import_file(conn: Any, path: Path) -> int:
    """Load a `courier.jsonl`'s lines into an empty log, in file order. A line's id is its position and text, so a
    second machine importing the same file adds nothing. Called on the write path only."""
    from harness import store_courier

    key = f"{id(conn)}:{path}"
    if key in _IMPORTED:
        return 0
    _IMPORTED.add(key)
    if store_courier.count(conn) > 0 or not Path(path).is_file():
        return 0
    rows = [
        (f"file:{n:09d}:{hashlib.sha256(line.encode('utf-8')).hexdigest()[:16]}", f"0000-file-{n:09d}", line)
        for n, line in enumerate(_file_lines(path))
    ]
    return store_courier.append_many(conn, rows)


def read_text(path: Path) -> str:
    """Edge, read only. The whole bus as JSON lines, oldest first (`readable`). A store that cannot be read is an
    empty bus, with a note."""
    from harness import store_courier

    try:
        conn = _read_conn(path)
        lines = readable(store_courier.lines(conn) if store_courier.exists(conn) else None, _file_lines(path))
    except Exception as exc:
        print(f"note: courier not read from the store ({type(exc).__name__}: {exc})", file=sys.stderr)
        return ""
    return "".join(f"{line}\n" for line in lines)


def append_line(path: Path, line: str) -> None:
    """Edge. Add one line to the bus. A failure raises: a message is never dropped quietly."""
    from harness import store_courier

    conn = _conn(path)
    _import_file(conn, path)
    store_courier.append(conn, uuid.uuid4().hex, _stamp(), line)


def line_count(path: Path) -> int | None:
    """Edge, read only. How many lines a read shows (`readable_count`); None when the store cannot be read."""
    from harness import store_courier

    try:
        conn = _read_conn(path)
        return readable_count(store_courier.count(conn) if store_courier.exists(conn) else None, len(_file_lines(path)))
    except Exception:
        return None
