"""The `attempts` reader for `chair_facts.FactsDeps` under work_state store: attempts read back from run task records.

Under work_state store the board is the store's rows, and nothing writes an attempt into a work file: a quarantined
task's exported ticket has no `attempts:`, and its queue row's `extra` holds none. The files path
(`chair_read_attempts.read_attempts`) then finds no cause, run or reason, and every quarantine reached the chair as
`QUARANTINED_CAUSE` with no run. The epic still saves each task's record as `runs/<run>/tasks/<phase>/<task>.json`, and
under a store a quarantine puts its cause on it (`core.run_record`). This reads those records for every task the store
holds open (quarantined, ready or blocked) and gives the rows the files path gives, so `chair_facts.quarantine_facts`
sees the cause, the run, the reason and the failure count.

A remote lane's run comes home through the chair's `fetch_exit` (or `fetch`) action: `cox runs fetch <run>` pulls the
run's whole `runs/<run>/` directory, task records included, into this runs directory (`remote_fetch.pull_argvs`). Until
that fetch, the record is on the lane host and in the store's `task_records` only, and this reader does not see it.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools.chair_facts import RESCUE_FAILED_CAUSE, _key, _run_number, run_initiative
from agent_tools.chair_read_attempts import read_attempts
from agent_tools.chair_read_cache import StampCache, file_stamp
from agent_tools.chair_read_quarantined import WorkFiles, body_sha
from core.run_record import RESCUE_KIND, record_attempt, record_body_sha

OPEN_STATES = ("quarantined", "ready", "blocked")

# (run, phase, task, record, the file's mtime as ISO-8601 UTC)
RecordFile = tuple[str, str, str, Mapping[str, Any], str]
Row = Mapping[str, Any]


def _open_bodies(store_rows: Iterable[Row]) -> dict[tuple[str, str, str], str]:
    """(initiative, phase, task) to the current body of every task row the store holds open."""
    return {
        (str(r.get("initiative") or ""), str(r.get("phase") or ""), str(r.get("task_id") or "")): str(r.get("body") or "")
        for r in store_rows
        if r.get("kind", "task") == "task" and r.get("state") in OPEN_STATES
    }


def open_record_keys(store_rows: Iterable[Row]) -> set[tuple[str, str]]:
    """The (phase, task) of every task row the store holds open: the record files worth opening."""
    return {(phase, task) for _, phase, task in _open_bodies(store_rows)}


def record_rows(records: Iterable[RecordFile], store_rows: Iterable[Row]) -> list[dict]:
    """Pure. One attempt row per record that names a cause, for a task the store holds open, oldest first.

    The row has the files path's keys: run (the run directory), initiative (the record's own, else the run's), phase,
    task, cause, cause_why, reason and kind when a rule names it (`core.run_record.record_attempt`), ts (the record's
    `date`, else the file's mtime) and path. `on_current_body` is False only when the record carries the body it was
    built on (`record_body_sha`) and that body is not the row's current one; a record that carries none counts. A failed
    rescue's cause is `RESCUE_FAILED_CAUSE`, as `chair_read_attempts.with_stored_rescues` names it.

    Records of one day tie on `date`, so the run's number, then the file's mtime, orders them."""
    bodies = _open_bodies(store_rows)
    found = []
    for run, phase, task, record, mtime in records:
        attempt = record_attempt(record)
        initiative = str(record.get("initiative") or "") or run_initiative(run)
        current = bodies.get((initiative, phase, task))
        if attempt is None or current is None:
            continue
        sha = record_body_sha(record)
        row = {
            **attempt,
            **({"cause": RESCUE_FAILED_CAUSE} if attempt.get("kind") == RESCUE_KIND else {}),
            "path": f"runs/{run}/tasks/{phase}/{task}.json",
            "run": run,
            "initiative": initiative,
            "phase": phase,
            "task": task,
            "ts": str(record.get("date") or "") or mtime,
            "on_current_body": sha is None or sha == body_sha(current),
            **({"body_sha": sha} if sha else {}),
        }
        found.append(((row["ts"], _run_number(run), mtime), row))
    return [row for _, row in sorted(found, key=lambda pair: pair[0])]


def merged_attempts(file_rows: Sequence[Row], records: Sequence[Row]) -> list[dict]:
    """Pure. The files path's rows, then each record row for a run the files path does not already name for its task,
    sorted by `ts` oldest first. A work file that holds an attempt wins over the record of the same run."""
    named = {(*_key(r), str(r.get("run") or "")) for r in file_rows}
    kept = [r for r in records if (*_key(r), str(r.get("run") or "")) not in named]
    return sorted([dict(r) for r in (*file_rows, *kept)], key=lambda row: str(row.get("ts") or ""))


def _parsed(path: Path) -> tuple[Mapping[str, Any], str] | None:
    """Edge. The record at `path` and its mtime as ISO-8601 UTC; None when it is unreadable or not a JSON object."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except (OSError, ValueError):
        return None
    return (record, mtime) if isinstance(record, dict) else None


def read_run_records(runs_dir: Path, wanted: Collection[tuple[str, str]], cache: StampCache | None = None) -> list[RecordFile]:
    """Edge. Every `runs_dir/<run>/tasks/<phase>/<task>.json` whose (phase, task) is in `wanted`, in path order.

    Only those files are opened. An unreadable file, or one that is not a JSON object, is left out. With a cache, a
    file whose (mtime_ns, size) stamp is unchanged is not parsed again."""
    paths = [p for p in sorted(runs_dir.glob("*/tasks/*/*.json")) if (p.parent.name, p.stem) in wanted]
    found = []
    for p in paths:
        parsed = _parsed(p) if cache is None else cache.get(p, file_stamp(p), lambda p=p: _parsed(p))
        if parsed is not None:
            found.append((p.parts[-4], p.parent.name, p.stem, parsed[0], parsed[1]))
    if cache is not None:
        cache.keep(paths)
    return found


def read_store_attempts(
    workspace: Path, files: WorkFiles, store_rows: Sequence[Row], cache: StampCache | None = None,
) -> list[dict]:
    """Edge. The files path's attempts (`chair_read_attempts.read_attempts`) with the attempts the run task records
    under `workspace/runs` hold for each task `store_rows` (the store's queue rows) holds open, oldest first."""
    records = read_run_records(workspace / "runs", open_record_keys(store_rows), cache)
    return merged_attempts(read_attempts(workspace, files), record_rows(records, store_rows))
