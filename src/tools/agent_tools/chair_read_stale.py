"""The `stale_candidates` reader: per-task file, run, chair-action and quarantine-cause history as `StaleCandidate` rows.

Reuses `chair_read_quarantined.read_work_items` to list the board, `chair_read_attempts.read_attempts` and
`chair_facts.task_key` for attempts matched exactly as `chair_facts.harness_failures` matches them, and
`chair_read_quarantined.attempts_on_current_body` for the current ticket version."""

import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import run_store
from agent_tools.chair_facts import HARNESS_CAUSE, LAUNCH_KINDS, Key, task_key
from agent_tools.chair_read_attempts import read_attempts
from agent_tools.chair_read_quarantined import WorkFiles, attempts_on_current_body, item_body, read_work_items
from agent_tools.chair_revert_watch import RESOLVED_KIND, WATCH_KIND
from agent_tools.chair_stall_alarm import ALARM_KINDS
from agent_tools.chair_types import StaleCandidate

Row = Mapping[str, object]

_EXCLUDED_STATES = frozenset({"done", "dropped", "draft"})
ACTIONS_WINDOW_DAYS = 7
# Read at any age: needs_chair dedupes decompose_stalled for good, steer_clear plus launches make the steer streak,
# and a revert_watch stays pending, and its verdict counts, however long ago it was recorded. A stall alarm stays raised
# until its clear, so a stall standing longer than the window is not raised again (chair_stall_alarm.open_alarms).
_KEPT_KINDS = ("needs_chair", "steer_clear", *sorted(LAUNCH_KINDS), WATCH_KIND, RESOLVED_KIND, *sorted(ALARM_KINDS))


def _iso(value: object) -> str:
    """YAML frontmatter and a Postgres store both hand back `datetime` for a timestamp; `str()` of one is not ISO-8601."""
    return value.isoformat() if isinstance(value, datetime) else str(value)


def last_run_at(attempts: Sequence[Row], key: Key) -> str | None:
    """The newest `ts` among the attempts `harness_failures` would count for `key`."""
    return max((_iso(a["ts"]) for a in attempts if task_key(a) == key and a.get("ts")), default=None)


def quarantine_non_harness_count(attempts: Sequence[Row], body: str, key: Key) -> int:
    """The complement of `harness_failures` for `key`, over the attempts made on the current `body` only."""
    on_body = attempts_on_current_body([a for a in attempts if task_key(a) == key], body)
    return sum(1 for a in on_body if a.get("cause") != HARNESS_CAUSE)


def _fills_or_matches(row: Row, column: str, value: str) -> bool:
    """A column the row lacks or leaves empty cannot tell tasks apart, so it matches."""
    return str(row.get(column) or value) == value


def last_chair_action_at(actions: Sequence[Row], key: Key) -> str | None:
    """The newest `ts` of rows targeting the task, else of rows targeting its initiative.

    A chair action names a task id, never a phase (`chair_types.Action`), so an `initiative` or `phase` column
    narrows the task match only where a row fills it."""
    initiative, phase, task = key
    on_task = [
        _iso(a["ts"])
        for a in actions
        if a.get("ts")
        and a.get("target") == task
        and _fills_or_matches(a, "initiative", initiative)
        and _fills_or_matches(a, "phase", phase)
    ]
    on_initiative = [_iso(a["ts"]) for a in actions if a.get("ts") and a.get("target") == initiative]
    return max(on_task or on_initiative, default=None)


def actions_query(token: str, now: datetime | None, window_days: int = ACTIONS_WINDOW_DAYS) -> tuple[str, tuple[str, ...]]:
    """The `chair_actions` SELECT and its bind values. With `now`, rows from `window_days` back, by date prefix so
    every ISO `ts` form sorts right, plus every row of `_KEPT_KINDS`; with no `now`, every row."""
    if now is None:
        return "SELECT * FROM chair_actions", ()
    marks = ", ".join(["{p}"] * len(_KEPT_KINDS))
    cutoff = (now - timedelta(days=window_days)).date().isoformat()
    return run_store.sql_for(f"SELECT * FROM chair_actions WHERE ts >= {{p}} OR kind IN ({marks})", token), (cutoff, *_KEPT_KINDS)


def read_chair_actions(runs_dir: Path, now: datetime | None = None, window_days: int = ACTIONS_WINDOW_DAYS) -> list[dict[str, object]]:
    """Edge. The `chair_actions` rows from the last `window_days` before `now`; every row when `now` is None.
    `[]` with no store, no table, or the driver absent.

    A table with no `target` column raises: read as `[]` it would make every task look staler than it is."""
    try:
        opened = run_store.open_store(runs_dir)
    except (*run_store._DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return []
    if opened is None:
        return []
    conn, token = opened
    sql, params = actions_query(token, now, window_days)
    try:
        cursor = conn.execute(sql, params)
        columns = [d[0] for d in cursor.description]
        rows = [dict(r) for r in cursor.fetchall()]
    except run_store._DB_ERRORS:  # no chair_actions table yet, so no history
        return []
    finally:
        conn.close()
    if "target" not in columns:
        raise RuntimeError(f"chair_actions has no target column, only {sorted(columns)}")
    return rows


GIT_LOG_TIMEOUT_S = 30


def newest_changes(log_text: str) -> dict[str, str]:
    """Pure. Path to the author date of the newest commit that names it, from `git log --format=%x00%aI --name-only`
    output, newest commit first: each record is a NUL, the date line, then one path per line."""
    newest: dict[str, str] = {}
    for record in log_text.split("\0")[1:]:
        date, *paths = record.split("\n")
        for path in paths:
            if path.strip():
                newest.setdefault(path.strip(), date.strip())
    return newest


def read_file_changes(workspace_dir: Path) -> dict[str, str] | None:
    """Edge. `newest_changes` for every ticket under `work/`, from one `git log` for the whole board; None when the
    workspace has no readable history. It replaced one `git log -1` per ticket, about 750 processes a tick."""
    try:
        done = subprocess.run(
            ["git", "-C", str(workspace_dir), "log", "--format=%x00%aI", "--name-only", "--no-renames", "--", "work"],
            capture_output=True,
            text=True,
            timeout=GIT_LOG_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return newest_changes(done.stdout) if done.returncode == 0 else None


def changed_at(changes: Mapping[str, str] | None, workspace_dir: Path, initiative: str, phase: str, task: str) -> str | None:
    """Edge. The ticket's newest commit date from `changes`; with none there, the file's own modification time, so a
    ticket written since the last workspace commit reads as fresh; None with neither."""
    rel = f"work/{initiative}/{phase}/{task}.md"
    found = (changes or {}).get(rel)
    if found:
        return found
    try:
        return datetime.fromtimestamp((workspace_dir / rel).stat().st_mtime, UTC).isoformat()
    except OSError:
        return None


def _candidate(
    workspace_dir: Path, key: Key, state: str, body: str, attempts: Sequence[Row], actions: Sequence[Row],
    changes: Mapping[str, str] | None,
) -> StaleCandidate:
    initiative, phase, task = key
    return {
        "initiative": initiative,
        "task_id": task,
        "state": state,
        "last_file_change": changed_at(changes, workspace_dir, initiative, phase, task),
        "last_run": last_run_at(attempts, key),
        "last_chair_action": last_chair_action_at(actions, key),
        "quarantine_non_harness_count": quarantine_non_harness_count(attempts, body, key),
    }


def read_stale_candidates(
    workspace_dir: Path,
    now: datetime,
    files: WorkFiles | None = None,
    attempts: Sequence[Row] | None = None,
    actions: Sequence[Row] | None = None,
) -> list[StaleCandidate]:
    """Edge. One row per board task not done, dropped or draft. `now` computes no age; `chair_stale.stale_reason` does.

    `files`, `attempts` and `actions` stand in for the work-file, attempts and `chair_actions` reads when a tick already
    holds them. Given `actions`, the caller owns the window; it must reach back at least `stale_days`."""
    attempts = attempts if attempts is not None else read_attempts(workspace_dir, files)
    actions = actions if actions is not None else read_chair_actions(workspace_dir / "runs")
    candidates = [(p, text, item) for p, text, item in read_work_items(workspace_dir, "files", files) if item["state"] not in _EXCLUDED_STATES]
    changes = read_file_changes(workspace_dir) if candidates else None
    return [
        _candidate(workspace_dir, (str(item["initiative"]), p.parts[-2], p.stem), str(item["state"]), item_body(text), attempts, actions, changes)
        for p, text, item in candidates
    ]
