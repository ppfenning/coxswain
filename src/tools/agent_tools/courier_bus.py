"""Where the courier bus is kept: the store's `courier_log` when the workspace has a shared store, else the file.

Callers name the bus by its old path, `<workspace>/courier.jsonl`, and go through `courier.read_text`,
`courier.append_file` and `courier.ack_file`. Those ask this module where the lines live. With a Postgres store the
bus is the `courier_log` table, one bus for every machine; a workspace with only its own local store keeps the file,
which is all one machine needs.

The first use of the table loads the lines a `courier.jsonl` already holds, once: nothing sent before the move is lost.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CONNECTIONS: dict[str, Any] = {}
_IMPORTED: set[str] = set()


def store_url(path: Path) -> str:
    """Edge. The store URL of the workspace whose bus is at `path`."""
    from agent_tools import run_store

    return run_store._store_url(Path(path).parent / "runs")


def uses_store(path: Path) -> bool:
    """Whether the bus at `path` lives in the store: only for a shared (Postgres) store."""
    try:
        return store_url(path).startswith(("postgresql", "postgres"))
    except Exception:
        return False


def _conn(path: Path) -> Any:
    """Edge. One migrated connection per store URL for the life of the process."""
    from harness.store_dialect import connect
    from harness.store_migrate import default_modules, migrate

    url = store_url(path)
    if url not in _CONNECTIONS:
        conn = connect(url)
        migrate(conn, _stamp(), default_modules())
        _CONNECTIONS[url] = conn
    return _CONNECTIONS[url]


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _import_file(conn: Any, path: Path) -> int:
    """Load a `courier.jsonl`'s lines into an empty log, in file order. A line's id is its position and text, so a
    second machine importing the same file adds nothing."""
    from harness import store_courier

    key = f"{id(conn)}:{path}"
    if key in _IMPORTED:
        return 0
    _IMPORTED.add(key)
    if store_courier.count(conn) > 0 or not Path(path).is_file():
        return 0
    lines = [ln for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    rows = [
        (f"file:{n:09d}:{hashlib.sha256(line.encode('utf-8')).hexdigest()[:16]}", f"0000-file-{n:09d}", line)
        for n, line in enumerate(lines)
    ]
    return store_courier.append_many(conn, rows)


def read_text(path: Path) -> str:
    """Edge. The whole bus as JSON lines, oldest first. A store that cannot be read is an empty bus, with a note."""
    from harness import store_courier

    try:
        conn = _conn(path)
        _import_file(conn, path)
        lines = store_courier.lines(conn)
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
    """Edge. How many lines the bus holds; None when the store cannot be read."""
    from harness import store_courier

    try:
        conn = _conn(path)
        _import_file(conn, path)
        return store_courier.count(conn)
    except Exception:
        return None
