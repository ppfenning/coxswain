"""Edge reader: the newest ended runs of each initiative, as `chair_no_progress.RunOutcome` rows, from the run store.

A run counts once its `runs` row has an `ended_at`. Its phase states come from its `phases` rows (`record_json`'s
`status` and `reason`, as the epic's `_record_phase` writes them), and its task states from its `tasks` rows (the
explicit `state` column the epic's `_record_tasks` writes, one row per task at its final state in the run). Only an
initiative's own series (`<initiative>-N`) is read, the one the chair launches and relaunches; a phase run
(`<initiative>.<phase>-N`) is a series of its own. An initiative whose newest run has not ended is left out: what it
will end with is not known yet. A missing or unreadable store reads as no runs, so the guard holds nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair_no_progress import RunOutcome
from core.workstore import run_series

READ_ERRORS = (*run_store._DB_ERRORS, RuntimeError, OSError)


def newest_runs(runs: Sequence[tuple[str, bool]], initiatives: Collection[str], last: int) -> dict[str, list[str]]:
    """Pure. `runs` are (run id, ended). Per initiative, its own series' newest `last` ended run ids, newest first. An
    initiative with no ended run, or whose newest run has not ended, is left out."""
    wanted = frozenset(initiatives)
    numbered: dict[str, list[tuple[int, str, bool]]] = {}
    for run, ended in runs:
        series, number = run_series(run)
        if series in wanted and number >= 0:
            numbered.setdefault(series, []).append((number, run, ended))
    found = {}
    for initiative, own in sorted(numbered.items()):
        ordered = sorted(own, reverse=True)
        if ordered[0][2]:
            found[initiative] = [run for _, run, ended in ordered if ended][:last]
    return found


def outcomes_from_rows(
    newest: Mapping[str, Sequence[str]], phase_rows: Sequence[Mapping[str, Any]], task_rows: Sequence[Mapping[str, Any]]
) -> dict[str, list[RunOutcome]]:
    """Pure. Each initiative's runs in `newest` order, with the status and reason of each of its phase rows and the
    `state` of each of its task rows. A phase record that does not decode reads as one with no status."""
    phases: dict[str, dict[str, str]] = {}
    reasons: dict[str, dict[str, str]] = {}
    tasks: dict[str, dict[str, str]] = {}
    for row in phase_rows:
        record = run_store.json_cell(row.get("record_json"))
        record = record if isinstance(record, dict) else {}
        phases.setdefault(str(row["run_id"]), {})[str(row["phase_id"])] = str(record.get("status") or "")
        reasons.setdefault(str(row["run_id"]), {})[str(row["phase_id"])] = str(record.get("reason") or "")
    for row in task_rows:
        tasks.setdefault(str(row["run_id"]), {})[str(row["task_id"])] = str(row.get("state") or "")
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
        opened = run_store.open_store(runs_dir)
        if opened is None:
            return {}
        conn, token = opened
        try:
            runs = [(str(r["run_id"]), bool(r["ended_at"])) for r in conn.execute("SELECT run_id, ended_at FROM runs")]
            newest = newest_runs(runs, initiatives, last)
            ids = [run for own in newest.values() for run in own]
            if not ids:
                return {}
            marks = ", ".join(["{p}"] * len(ids))
            phase_rows = conn.execute(
                run_store.sql_for(f"SELECT run_id, phase_id, record_json FROM phases WHERE run_id IN ({marks})", token), ids
            ).fetchall()
            task_rows = conn.execute(
                run_store.sql_for(f"SELECT run_id, task_id, state FROM tasks WHERE run_id IN ({marks})", token), ids
            ).fetchall()
        finally:
            conn.close()
    except READ_ERRORS:
        return {}
    return outcomes_from_rows(newest, [dict(r) for r in phase_rows], [dict(r) for r in task_rows])
