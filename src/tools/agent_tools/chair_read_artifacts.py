"""The `artifacts_missing` reader: the step rows of the last day whose `result_json` names an upload error.

The worker marks a step row whose artifacts were not all written (`harness.artifacts.mark_missing`); the chair, which
reads the store, raises the operator's alarm from these rows (`chair_stall_alarm.missing_runs`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from agent_tools import run_store
from agent_tools.chair_stall_alarm import MISSING_WINDOW
from core.jsontypes import is_mapping

__all__ = ["read_marked_steps"]


def read_marked_steps(runs_dir: Path, now: datetime) -> list[Mapping[str, object]]:
    """Edge. Step rows finished within `MISSING_WINDOW` of `now` whose `result_json` holds `artifacts_error`, as
    {run_id, initiative, finished_at, result_json}. Empty, never raising, with no store, no `step_results` table (a store
    before migration 0020), an unreachable Postgres, or the driver absent."""
    try:
        opened = run_store.open_store(runs_dir)
    except (*run_store._DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return []
    if opened is None:
        return []
    conn, token = opened
    cutoff = (now - MISSING_WINDOW).isoformat()
    try:
        sql = run_store.sql_for(
            "SELECT run_id, initiative, finished_at, result_json FROM step_results WHERE finished_at >= {p}", token
        )
        rows = conn.execute(sql, (cutoff,)).fetchall()
    except run_store._DB_ERRORS:
        return []
    finally:
        conn.close()
    found: list[Mapping[str, object]] = []
    for row in rows:
        result = run_store.json_cell(row["result_json"])
        if is_mapping(result) and result.get("artifacts_error"):
            found.append({"run_id": row["run_id"], "initiative": row["initiative"], "finished_at": row["finished_at"],
                          "result_json": dict(result)})
    return found
