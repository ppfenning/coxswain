"""`route remove` core: retire a queued intake, or drop an initiative nothing has started. The edge writes; the name, date and reason come in as arguments."""

import fnmatch
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from agent_tools import draft_apply, route_guard


def removed_line(by: str, date: str, reason: str) -> str:
    """`date` is an ISO day. Whitespace in the reason collapses so the record stays one line."""
    return f"removed by {by} on {date}: {' '.join(reason.split())}"


def intake_done_text(text: str, by: str, date: str, reason: str) -> str:
    """The intake text with the removal line appended, in the file's own line ending."""
    eol = "\r\n" if "\r\n" in text else "\n"
    body = text if not text or text.endswith("\n") else text + eol
    return body + removed_line(by, date, reason) + eol


def _refuse(reason: str) -> int:
    print(f"remove: refused: {reason}")
    return route_guard.REFUSED


def _remove_intake(ws: Path, task_id: str, by: str, date: str, reason: str, dry_run: bool) -> int:
    source, target = ws / "intake" / f"{task_id}.md", ws / "intake" / "done" / f"{task_id}.md"
    if not source.is_file():
        return _refuse(f"no intake file at {source}")
    if target.exists():
        return _refuse(f"{target} already exists")
    line = removed_line(by, date, reason)
    if dry_run:
        print(f"would remove intake {task_id}: move to {target}, adding: {line}")
        return route_guard.DONE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(intake_done_text(source.read_bytes().decode("utf-8"), by, date, reason).encode("utf-8"))
    source.unlink()
    print(f"remove: intake {task_id}: moved to {target}: {line}")
    return route_guard.DONE


def _tasks_of(rows: Sequence[Mapping], initiative: str) -> list[Mapping]:
    return [row for row in rows if row.get("kind", "task") == "task" and row.get("initiative") == initiative]


def remove(
    ws: Path, id: str, reason: str, by: str, now: datetime, rows: Sequence[Mapping], live_runs: Collection[str],
    store: draft_apply.Store, *, dry_run: bool = False,
) -> int:
    """Edge. `rows` are queue rows and `live_runs` the ids of runs live on the initiative. Exit 0 done, 2 refused with nothing written.

    An initiative goes through draft_apply.remove, which is decline's path: there is no second writer.
    """
    if not reason.strip():
        return _refuse("a removal needs a reason")
    target = route_guard.resolve_target(rows, id)
    if isinstance(target, route_guard.NotFound):
        return _refuse(f"no intake or initiative named {id}")
    if isinstance(target, route_guard.QueuedIntake):
        return _remove_intake(ws, target.task_id, by, now.date().isoformat(), reason, dry_run)
    guard = route_guard.initiative_guard(_tasks_of(rows, target.initiative), live_runs)
    if isinstance(guard, route_guard.Refusal):
        return _refuse(guard.reason)
    return draft_apply.remove(ws / "work", target.initiative, reason, by, store, dry_run=dry_run, clock=lambda: now)


def expand_targets(rows: Sequence[Mapping], ids: Sequence[str], match: str | None) -> tuple[list[str], str | None]:
    """Explicit ids in order, then queued intakes whose file stem or frontmatter id matches the glob. A glob matching nothing gives [] and a reason.

    A queued intake is listed once, under its file stem, however it was named.
    """
    queued = [row for row in rows if row.get("kind") == "intake" and row.get("state") == "queued"]
    matched = [
        row["task_id"]
        for row in queued
        if match is not None and any(fnmatch.fnmatchcase(name, match) for name in _names(row))
    ]
    if match is not None and not matched:
        return [], f"no queued intake matches {match}"
    targets = list(dict.fromkeys([*(_stem(rows, id) for id in ids), *matched]))
    return targets, (None if targets else "no targets given")


def _names(row: Mapping) -> list[str]:
    """The names route_guard accepts for a queued intake: the file stem and the frontmatter id."""
    return [name for name in (row["task_id"], (row.get("extra") or {}).get("id")) if isinstance(name, str)]


def _stem(rows: Sequence[Mapping], id: str) -> str:
    """A queued intake named by its frontmatter id becomes its file stem, so the same intake is not removed twice. Anything else passes through."""
    target = route_guard.resolve_target(rows, id)
    return target.task_id if isinstance(target, route_guard.QueuedIntake) else id


def remove_many(
    ws: Path,
    ids: Sequence[str],
    reason: str,
    by: str,
    now: datetime,
    rows: Sequence[Mapping],
    live_runs: Mapping[str, Collection[str]],
    store: draft_apply.Store,
    *,
    match: str | None = None,
    dry_run: bool = False,
) -> int:
    """Edge. `live_runs` maps an initiative to the ids of runs live on it; each target sees only its own. A refused target does not stop the rest. Returns the worst exit code."""
    targets, why = expand_targets(rows, ids, match)
    if not targets:
        return _refuse(why or "no targets given")
    codes = [
        remove(ws, target, reason, by, now, rows, live_runs.get(target, ()), store, dry_run=dry_run)
        for target in targets
    ]
    done = sum(1 for code in codes if code == route_guard.DONE)
    print(f"{'would remove' if dry_run else 'removed'} {done} of {len(codes)}")
    return max(codes)
