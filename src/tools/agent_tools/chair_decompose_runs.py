"""An intake's decompose runs as DecomposeRun values, oldest first."""
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair_decompose_streak import DecomposeRun
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_read_cache import Stamp, StampCache, file_stamp
from agent_tools.chair_read_exits import exit_rows, row_exited
from agent_tools.store_dialect import is_postgres

REFUSAL_MARKERS = ("approved but not executed", "initiative-decompose failed:")
LINT_REFUSAL_MARKER = "initiative-decompose failed:"  # the reach lint's refusal; such a run wrote nothing


def refusal_line(log: str) -> str | None:
    return next((line for line in log.splitlines() if any(m in line for m in REFUSAL_MARKERS)), None)


def refusal_reason(log: str) -> str | None:
    """The refusal line, plus the next line after ` — ` when that line is non-empty and not indented."""
    line = refusal_line(log)
    if line is None:
        return None
    lines = log.splitlines()
    following = lines[lines.index(line) + 1 :][:1]
    if following and following[0] and not following[0][0].isspace():
        return f"{line} — {following[0]}"
    return line


def log_wrote_items(run_id: str, log: str) -> bool | None:
    """A lint refusal means False, a `recorded <run_id>:` line means its proposal count is above zero, else None.

    None means the log cannot say (an older or unreadable log), so the caller falls back to the store count."""
    if LINT_REFUSAL_MARKER in log:
        return False
    recorded = re.search(
        rf"recorded {re.escape(run_id)}: \d+ auto-applied, \d+ gated decision\(s\), (\d+) proposal\(s\)", log
    )
    return int(recorded.group(1)) > 0 if recorded else None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_run(record: Any) -> DecomposeRun:
    """The run's own log decides wrote_items; `task_items`, the initiative's task count (None means unknown), is the fallback.

    The fallback reads zero as a run that wrote nothing, since no run can have created an item the store lacks.
    Unknown, or an unreadable record, reads as wrote_items True, so it never joins a streak or blocks an intake."""
    get = record.get if isinstance(record, Mapping) else (lambda key: None)
    run_id, log, items = get("run_id"), get("log"), get("task_items")
    readable = isinstance(run_id, str) and (log is None or isinstance(log, str)) and (items is None or _is_count(items))
    if not readable:
        return DecomposeRun(run_id=run_id if isinstance(run_id, str) else "", wrote_items=True, refusal_line=None)
    verdict = log_wrote_items(run_id, log or "")
    return DecomposeRun(
        run_id=run_id, wrote_items=items != 0 if verdict is None else verdict, refusal_line=refusal_line(log or "")
    )


def select_runs(rows: Sequence[Mapping[str, Any]], intake_id: str) -> list[Mapping[str, Any]]:
    """The `runs` rows whose id is `<intake_id>-<n>`, oldest `launched_at` first."""
    own = (r for r in rows if run_initiative(str(r.get("run_id") or "")) == intake_id)
    return sorted(own, key=lambda r: str(r.get("launched_at") or ""))


# Deliberate process-lifetime cache, a bend of charter A4: one parse per (run_id, log mtime_ns).
_REFUSALS: dict[tuple[str, int], tuple[str | None, bool | None]] = {}


def _parse_log(path: Path, run_id: str) -> tuple[str | None, bool | None]:
    """Edge. One read and parse of a run's log; (None, None) if it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (None, None)
    return (refusal_line(text), log_wrote_items(run_id, text))


def _log_facts(runs_dir: Path, run_id: str, cache: StampCache | None = None) -> tuple[str | None, bool | None]:
    """Edge. The log's (refusal line, wrote_items verdict), parsed once per (run_id, mtime_ns); (None, None) if missing.

    With a cache the entry is keyed by the log's file stamp instead, so a log still being written is re-read."""
    path = runs_dir / f"{run_id}.log"
    if cache is not None:
        return cache.get(_log_key(runs_dir, run_id), file_stamp(path), lambda: _parse_log(path, run_id))
    try:
        key = (run_id, path.stat().st_mtime_ns)
        if key not in _REFUSALS:
            text = path.read_text(encoding="utf-8", errors="replace")
            _REFUSALS[key] = (refusal_line(text), log_wrote_items(run_id, text))
        return _REFUSALS[key]
    except OSError:
        return (None, None)


def _task_items(runs_dir: Path, initiative: str) -> int | None:
    """None when the harness python is missing, since `run_store.work_items` then reads as empty."""
    if run_store._harness_python() is None:
        return None
    return sum(1 for row in run_store.work_items(runs_dir, initiative) if row.get("kind", "task") == "task")


def _log_key(runs_dir: Path, run_id: str) -> tuple[str, str, str]:
    return ("decompose-log", str(runs_dir), run_id)


def _store_files(runs_dir: Path) -> list[Path]:
    """Edge. The SQLite store file and its `-wal` sidecar; empty for Postgres, which has no file to stamp."""
    url = run_store._store_url(runs_dir)
    if is_postgres(url):
        return []
    db = run_store._sqlite_path(url)
    return [db, db.with_name(db.name + "-wal")]


def _store_stamp(runs_dir: Path) -> Stamp | None:
    """Edge. One stamp over the store file and its WAL, since a write may touch only the WAL.

    None when the store has no file or the main file is missing, so that read is never cached."""
    stamps = [file_stamp(path) for path in _store_files(runs_dir)]
    if not stamps or stamps[0] is None:
        return None
    present = [s for s in stamps if s is not None]
    return (max(s[0] for s in present), sum(s[1] for s in present))


def read_decompose_runs(runs_dir: Path, intake_id: str, cache: StampCache | None = None) -> list[DecomposeRun]:
    """Edge. A run that has not exited gets `task_items` None; the intake id is the `chair_exec.decompose_id` value.

    With a cache, the exit rows, the task count and each run's log facts are keyed by file stamp, so an unchanged
    file is not read again. Entries not seen in this call are dropped, so give each intake its own cache."""
    if cache is None:
        items = _task_items(runs_dir, intake_id)
        rows = exit_rows(runs_dir)
    else:
        stamp = _store_stamp(runs_dir)
        exits_key, items_key = ("decompose-exits", str(runs_dir)), ("decompose-items", str(runs_dir), intake_id)
        # a missing harness python reads as unknown and is judged on every call, never stored
        items = (
            None
            if run_store._harness_python() is None
            else cache.get(items_key, stamp, lambda: _task_items(runs_dir, intake_id))
        )
        rows = cache.get(exits_key, stamp, lambda: exit_rows(runs_dir))
    out = []
    for row in select_runs(rows, intake_id):
        exited = row_exited(row)
        run = parse_run({"run_id": str(row["run_id"]), "log": None, "task_items": items if exited else None})
        refusal, verdict = _log_facts(runs_dir, run.run_id, cache)
        wrote = verdict if exited and verdict is not None else run.wrote_items
        out.append(replace(run, wrote_items=wrote, refusal_line=refusal))
    if cache is not None:
        cache.keep([exits_key, items_key, *(_log_key(runs_dir, run.run_id) for run in out)])
    return out


def _read_log(runs_dir: Path, run_id: str) -> str | None:
    try:
        return (runs_dir / f"{run_id}.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def decompose_refusals(runs_dir: Path, run_ids: Iterable[str]) -> dict[str, str]:
    """Edge. Run id to its refusal reason; a missing or unreadable log, or one with no refusal, is left out."""
    texts = {run_id: _read_log(runs_dir, run_id) for run_id in run_ids}
    reasons = {run_id: refusal_reason(text) for run_id, text in texts.items() if text is not None}
    return {run_id: reason for run_id, reason in reasons.items() if reason is not None}


def live_decompose_initiatives(runs_dir: Path) -> frozenset[str]:
    """Edge. Initiatives with a live `<run_id>.pid` process; epic-build pid files count too, holding conservatively."""
    from agent_tools.chair_read_lost import _pid_alive  # lazy: chair_read_lost imports chair_facts

    try:
        pidfiles = sorted(runs_dir.glob("*.pid"))
    except OSError:
        return frozenset()
    return frozenset(run_initiative(path.stem) for path in pidfiles if _pid_alive(path))
