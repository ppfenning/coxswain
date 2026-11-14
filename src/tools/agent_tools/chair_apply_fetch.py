"""Fold a fetched run's approvals into local ticket state.

`chair_exec`'s `fetch_exit` action runs `cox runs fetch` for a remote run, then hands the run
and its initiative here. The run's task records are already on disk under `runs/<run>/tasks/`;
this reads them, decides which are approved by the same rule `land.py` applies before a land,
and moves each approved task's ticket file from `ready` to `approved`. The store row is the board,
so each approved task is also mirrored to `approved` through `store_cli.mirror_state`; a mirror
warning is returned beside the ids and never raised.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from agent_tools import queue_export, store_cli
from agent_tools.land import _approved

__all__ = ["apply_fetched_approvals", "apply_ready_to_approved", "approved_task_ids"]


def _task_id(record: Mapping[str, Any]) -> str:
    """A task record written by the harness names its task under `ticket`; `task` is kept for older readers."""
    return str(record.get("task") or record.get("ticket") or "")


def approved_task_ids(records: Iterable[Mapping[str, Any]]) -> list[str]:
    """The task id of every record whose review and arbitration verdicts approve it (`land._approved` is None)."""
    return [_task_id(record) for record in records if _task_id(record) and _approved(dict(record)) is None]


def apply_ready_to_approved(text: str) -> tuple[str | None, str | None]:
    """The ticket's `state: ready` line rewritten to `state: approved`, paired with `None`; or `None` paired
    with `None` when the state is already `approved` or `done` (nothing to do); or `None` paired with a
    one-line refusal naming the state when it is anything else. Mirrors `land.approve_to_done`'s header scan."""
    if not text.startswith("---\n"):
        return None, "chair_apply_fetch: ticket has no state field"
    close = text.find("\n---\n", 4)
    header = text[:close] if close != -1 else text
    for line in header.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.startswith("state:"):
            continue
        state = stripped[len("state:"):].strip()
        if state in ("approved", "done"):
            return None, None
        if state != "ready":
            return None, f"chair_apply_fetch: ticket state is {state!r}, not moving to approved"
        return text.replace(line, line.replace("ready", "approved", 1), 1), None
    return None, "chair_apply_fetch: ticket has no state field"


def _task_records(runs_dir: Path, run: str) -> list[dict[str, Any]]:
    """Edge. Every parseable dict under `runs/<run>/tasks/<phase>/<task>.json`; a bad file is skipped, not raised."""
    records = []
    for path in sorted((runs_dir / run / "tasks").glob("*/*.json")):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _ticket_path(work_dir: Path, initiative: str, task_id: str) -> Path | None:
    """Edge. The one ticket file under `work/<initiative>/**/<task_id>.md`; None when none matches."""
    return next(iter(sorted((work_dir / "work" / initiative).glob(f"**/{task_id}.md"))), None)


def _approve_ticket_file(work_dir: Path, workspace: Path, initiative: str, task_id: str) -> tuple[bool, str | None]:
    """Edge. Whether to mirror the task, and a warning when its ticket file could not be read or written.

    A missing ticket file is mirrored without a warning. Only a ticket whose state is neither ready, approved
    nor done is not mirrored, since the file itself says the task is not approved. A file this writes is recorded
    in `workspace`'s export baseline, so an export racing it does not read the write as a hand edit.
    """
    path = _ticket_path(work_dir, initiative, task_id)
    if path is None:
        return True, None
    try:
        new_text, refusal = apply_ready_to_approved(path.read_text(encoding="utf-8"))
        if refusal is not None:
            return False, None
        if new_text is not None:
            path.write_text(new_text, encoding="utf-8")
            queue_export.record_written_paths(workspace, [path])
    except OSError as exc:
        return True, f"warning: {task_id}: ticket file not updated: {type(exc).__name__}: {exc}"
    return True, None


def apply_fetched_approvals(runs_dir: Path, work_dir: Path, run: str, initiative: str) -> tuple[list[str], list[str]]:
    """Edge. Mirror every approved task to the store, moving its ticket file from ready first. Never raises.

    Returns the ids mirrored, once each in record order, and the warnings from file errors and from `mirror_state`.
    """
    ids: list[str] = []
    warnings: list[str] = []
    workspace = runs_dir.parent
    for task_id in dict.fromkeys(approved_task_ids(_task_records(runs_dir, run))):
        mirror, file_warning = _approve_ticket_file(work_dir, workspace, initiative, task_id)
        warnings.extend([] if file_warning is None else [file_warning])
        if not mirror:
            continue
        mirror_warning = store_cli.mirror_state(runs_dir, initiative, task_id, "approved", "chair")
        warnings.extend([] if mirror_warning is None else [mirror_warning])
        ids.append(task_id)
    return ids, warnings
