"""The store rows the dash feed history is built from: ended runs, and the task records and call rows of those runs."""

from pathlib import Path
from typing import Any

from agent_tools.run_store import _DB_ERRORS, open_store, sql_for

RECENT_ENDED = 30

_RUNS = (
    "SELECT * FROM runs WHERE ended_at IS NOT NULL AND (ended_at >= {p} OR run_id IN "
    "(SELECT run_id FROM runs WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT " + str(RECENT_ENDED) + ")) "
    "ORDER BY ended_at DESC"
)


_OPEN_TASK_RECORDS = (
    "SELECT * FROM task_records WHERE updated_at >= {p} AND run_id IN (SELECT run_id FROM runs WHERE ended_at IS NULL)"
)
_TASK_RECORDS_SINCE = "SELECT * FROM task_records WHERE updated_at >= {p}"


def _task_records(store: Path, sql: str, since: str) -> list[dict[str, Any]]:
    """Edge. The task records `sql` selects for `since`; empty with no store, no such table or column, or an
    unreadable store."""
    try:
        opened = open_store(store)
    except (*_DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return []
    if opened is None:
        return []
    conn, token = opened
    try:
        return [dict(r) for r in conn.execute(sql_for(sql, token), (since,)).fetchall()]
    except _DB_ERRORS:
        return []
    finally:
        conn.close()


def read_open_task_records(store: Path, since: str) -> list[dict[str, Any]]:
    """Edge. The task records of runs not yet ended, written at or after `since` (ISO text), column names unchanged.
    `read_history_rows` reads ended runs only, and a task merged into its phase or PR branch completes while its run is
    still in flight, so the spend windows read these beside it. Empty with no store or an unreadable one."""
    return _task_records(store, _OPEN_TASK_RECORDS, since)


def read_task_records_since(store: Path, since: str) -> list[dict[str, Any]]:
    """Edge. Every task record written at or after `since` (ISO text), ended run or not: what today's completion count
    reads. Empty with no store or an unreadable one."""
    return _task_records(store, _TASK_RECORDS_SINCE, since)


def _empty() -> dict[str, list[dict[str, Any]]]:
    return {"runs": [], "task_records": [], "node_calls": []}


def read_history_rows(store: Path, since: str) -> dict[str, list[dict[str, Any]]]:
    """Edge. {runs, task_records, node_calls} as the store holds them, column names unchanged. `runs` are ended runs with `ended_at >= since` (ISO text) or among the newest 30 ended; the other two cover those run ids only. Empty with no store or an unreadable one."""
    try:
        opened = open_store(store)
    except (*_DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return _empty()
    if opened is None:
        return _empty()
    conn, token = opened
    try:
        runs = [dict(r) for r in conn.execute(sql_for(_RUNS, token), (since,)).fetchall()]
        ids = tuple(run["run_id"] for run in runs)
        if not ids:
            return _empty()
        marks = ", ".join("{p}" for _ in ids)
        by_ids = {
            table: [dict(r) for r in conn.execute(sql_for(f"SELECT * FROM {table} WHERE run_id IN ({marks})", token), ids).fetchall()]
            for table in ("task_records", "node_calls")
        }
    except _DB_ERRORS:
        return _empty()
    finally:
        conn.close()
    return {"runs": runs, **by_ids}
