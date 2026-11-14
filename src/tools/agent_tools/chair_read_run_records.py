"""The `attempts` reader for `chair_facts.FactsDeps` under work_state store: attempts read back from run task records.

Under work_state store the board is the store's rows, and nothing writes an attempt into a work file: a quarantined
task's exported ticket has no `attempts:`, and its queue row's `extra` holds none. The files path
(`chair_read_attempts.read_attempts`) then finds no cause, run or reason, and every quarantine reached the chair as
`QUARANTINED_CAUSE` with no run. The epic still saves each task's record as `runs/<run>/tasks/<phase>/<task>.json`, and
under a store a quarantine puts its cause, and the body it was built on, on it (`core.run_record`). This reads those
records for every task the store holds open (quarantined, ready or blocked) and gives the rows the files path gives, so
`chair_facts.quarantine_facts` sees the cause, the run, the reason and the failure count.

A remote lane's run comes home through the chair's `fetch_exit` (or `fetch`) action: `cox runs fetch <run>` pulls the
run's whole `runs/<run>/` directory, task records included, into this runs directory (`remote_fetch.pull_argvs`). Until
that fetch, the record is on the lane host and in the store's `task_records` only, and this reader does not see it;
`chair_facts.awaiting_fetch` holds back the quarantines that fetch may change.
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
RECORD_SOURCE = "run_record"  # the `source` of a row read from a run task record, not from a work file

# (run, phase, task, the record's summary (`record_summary`), the file's mtime as ISO-8601 UTC)
RecordFile = tuple[str, str, str, Mapping[str, Any], str]
Row = Mapping[str, Any]


def record_summary(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Pure. What the chair keeps of one run task record: its attempt (`core.run_record.record_attempt`), its own
    initiative and date, and the body it was built on when it names one. None for a record that names no cause.

    A record runs to a hundred kilobytes and more; this is the few hundred bytes of it an attempt row is made from."""
    attempt = record_attempt(record)
    if attempt is None:
        return None
    sha = record_body_sha(record)
    return {
        **attempt,
        "initiative": str(record.get("initiative") or ""),
        "date": str(record.get("date") or ""),
        **({"body_sha": sha} if sha else {}),
    }


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


def _order(row: Row) -> tuple[int, str]:
    """A run's number first: one initiative's runs never overlap (its `runs:<initiative>` lease), so a later number is a
    later attempt whatever its record is dated. `ts` only orders rows of the same run."""
    return _run_number(str(row.get("run") or "")), str(row.get("ts") or "")


def record_rows(records: Iterable[RecordFile], store_rows: Iterable[Row]) -> list[dict]:
    """Pure. One attempt row per summarized record, for a task the store holds open, oldest first (`_order`).

    The row has the files path's keys: run (the run directory), initiative (the record's own, else the run's), phase,
    task, cause, cause_why, reason and kind when a rule names it, ts (the record's `date`, else the file's mtime) and
    path, plus `source` = RECORD_SOURCE. `on_current_body` is False only when the record names the body it was built on
    (`body_sha`) and that body is not the row's current one; a record that names none counts. A failed rescue's cause is
    `RESCUE_FAILED_CAUSE`, as `chair_read_attempts.with_stored_rescues` names it."""
    bodies = _open_bodies(store_rows)
    found = []
    for run, phase, task, summary, mtime in records:
        initiative = str(summary.get("initiative") or "") or run_initiative(run)
        current = bodies.get((initiative, phase, task))
        if current is None:
            continue
        sha = summary.get("body_sha")
        row = {
            **{k: v for k, v in summary.items() if k not in ("initiative", "date")},
            **({"cause": RESCUE_FAILED_CAUSE} if summary.get("kind") == RESCUE_KIND else {}),
            "path": f"runs/{run}/tasks/{phase}/{task}.json",
            "source": RECORD_SOURCE,
            "run": run,
            "initiative": initiative,
            "phase": phase,
            "task": task,
            "ts": str(summary.get("date") or "") or mtime,
            "on_current_body": not sha or sha == body_sha(current),
        }
        found.append(((*_order(row), mtime), row))
    return [row for _, row in sorted(found, key=lambda pair: pair[0])]


def merged_attempts(file_rows: Sequence[Row], records: Sequence[Row]) -> list[dict]:
    """Pure. The files path's rows, then each record row for a run the files path does not already name for its task,
    oldest first by run, then `ts` (`_order`). A work file that holds an attempt wins over the record of the same run."""
    named = {(*_key(r), str(r.get("run") or "")) for r in file_rows}
    kept = [r for r in records if (*_key(r), str(r.get("run") or "")) not in named]
    return sorted([dict(r) for r in (*file_rows, *kept)], key=_order)


def stale_attempts(rows: Iterable[Row]) -> list[Row]:
    """The rows the stale reader may count: a record row that names no body is left out.

    `chair_read_stale.quarantine_non_harness_count` counts the attempts on the current body, and such a record cannot be
    told apart from it: two failures either side of an amended ticket would count as two on the new body."""
    return [r for r in rows if r.get("source") != RECORD_SOURCE or r.get("body_sha")]


def _summarized(path: Path) -> dict[str, Any] | None:
    """Edge. `{"attempt": record_summary(...), "mtime": ...}` for the record at `path`; None when it is unreadable or not a
    JSON object. The attempt is None for a record that names no cause, which is still worth remembering."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except (OSError, ValueError):
        return None
    return {"attempt": record_summary(record), "mtime": mtime} if isinstance(record, dict) else None


def read_run_records(runs_dir: Path, wanted: Collection[tuple[str, str]], cache: StampCache | None = None) -> list[RecordFile]:
    """Edge. The summary of every `runs_dir/<run>/tasks/<phase>/<task>.json` whose (phase, task) is in `wanted` and whose
    record names a cause, in path order.

    Only those files are opened. An unreadable file, or one that is not a JSON object, is left out. With a cache, a file
    whose (mtime_ns, size) stamp is unchanged is not parsed again; the cache holds the summary, never the record."""
    paths = [p for p in sorted(runs_dir.glob("*/tasks/*/*.json")) if (p.parent.name, p.stem) in wanted]
    found = []
    for p in paths:
        read = _summarized(p) if cache is None else cache.get(p, file_stamp(p), lambda p=p: _summarized(p))
        if read is not None and read["attempt"] is not None:
            found.append((p.parts[-4], p.parent.name, p.stem, read["attempt"], read["mtime"]))
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
