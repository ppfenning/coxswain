"""Edge reader: the newest ended runs of each initiative, as `chair_no_progress.RunOutcome` rows, from the run store.

A run counts once its `runs` row has an `ended_at`; its phase states come from its `phases` rows and its task states
from its `task_records` rows. Only an initiative's own series (`<initiative>-N`) is read, the one the chair launches
and relaunches; a phase run (`<initiative>.<phase>-N`) is a series of its own. A missing or unreadable store reads as
no runs, so the guard holds nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair_no_progress import RunOutcome
from core.workstore import run_series

READ_ERRORS = (*run_store._DB_ERRORS, RuntimeError, OSError)


def newest_runs(run_ids: Sequence[str], initiatives: Collection[str], last: int) -> dict[str, list[str]]:
    """Pure. Per initiative, its own series' newest `last` run ids, newest first; an initiative with none is left out."""
    wanted = frozenset(initiatives)
    numbered: dict[str, list[tuple[int, str]]] = {}
    for run in run_ids:
        series, number = run_series(run)
        if series in wanted and number >= 0:
            numbered.setdefault(series, []).append((number, run))
    return {i: [run for _, run in sorted(runs, reverse=True)[:last]] for i, runs in sorted(numbered.items())}


def outcomes_from_rows(
    newest: Mapping[str, Sequence[str]], phase_rows: Sequence[Mapping[str, Any]], task_rows: Sequence[Mapping[str, Any]]
) -> dict[str, list[RunOutcome]]:
    """Pure. Each initiative's runs in `newest` order, with the status and reason of each of its phase rows and the
    `status` (else `state`) of each of its task records. A record that does not decode is read as an empty one."""
    phases: dict[str, dict[str, str]] = {}
    reasons: dict[str, dict[str, str]] = {}
    tasks: dict[str, dict[str, str]] = {}
    for row in phase_rows:
        record = run_store._json_cell(row.get("record_json"))
        record = record if isinstance(record, dict) else {}
        phases.setdefault(str(row["run_id"]), {})[str(row["phase_id"])] = str(record.get("status") or "")
        reasons.setdefault(str(row["run_id"]), {})[str(row["phase_id"])] = str(record.get("reason") or "")
    for row in task_rows:
        record = run_store._json_cell(row.get("record_json"))
        record = record if isinstance(record, dict) else {}
        tasks.setdefault(str(row["run_id"]), {})[str(row["task_id"])] = str(record.get("status") or record.get("state") or "")
    return {
        initiative: [
            {"run_id": run, "phases": phases.get(run, {}), "reasons": reasons.get(run, {}), "tasks": tasks.get(run, {})}
            for run in runs
        ]
        for initiative, runs in newest.items()
    }


def read_run_outcomes(runs_dir: Path, initiatives: Collection[str], last: int) -> dict[str, list[RunOutcome]]:
    """Edge. Per initiative, its newest `last` ended runs, newest first. {} with no store, nothing asked or `last` 0."""
    if not initiatives or last <= 0:
        return {}
    try:
        opened = run_store._open(runs_dir)
        if opened is None:
            return {}
        conn, token = opened
        try:
            ended = [str(r["run_id"]) for r in conn.execute("SELECT run_id FROM runs WHERE ended_at IS NOT NULL AND ended_at <> ''")]
            newest = newest_runs(ended, initiatives, last)
            ids = [run for runs in newest.values() for run in runs]
            if not ids:
                return {}
            marks = ", ".join(["{p}"] * len(ids))
            phase_rows = conn.execute(
                run_store._sql(f"SELECT run_id, phase_id, record_json FROM phases WHERE run_id IN ({marks})", token), ids
            ).fetchall()
            task_rows = conn.execute(
                run_store._sql(f"SELECT run_id, task_id, record_json FROM task_records WHERE run_id IN ({marks})", token), ids
            ).fetchall()
        finally:
            conn.close()
    except READ_ERRORS:
        return {}
    return outcomes_from_rows(newest, [dict(r) for r in phase_rows], [dict(r) for r in task_rows])
