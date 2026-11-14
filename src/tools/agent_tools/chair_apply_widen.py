"""Apply a `widen_ticket` action to its ticket file; `chair_exec._widen_ticket` mirrors the `ready` state to the store."""
from __future__ import annotations

from pathlib import Path

from agent_tools import queue_export, queue_rows, run_store
from agent_tools.chair_types import Action
from agent_tools.chair_widen_ticket import widen_ticket_text

__all__ = ["apply_widen"]


def _ticket_path(work_dir: Path, initiative: str, task_id: str) -> Path | None:
    """Edge. The one ticket file under `work/<initiative>/**/<task_id>.md`; None when none matches."""
    return next(iter(sorted((work_dir / "work" / initiative).glob(f"**/{task_id}.md"))), None)


def _mirror_to_store(runs_dir: Path, work_dir: Path, path: Path, initiative: str, task_id: str, text: str) -> str:
    """Edge. Store the written file's surfaces and body on its task row; "" once stored, else why it was not."""
    row = queue_rows.parse_item("task", path.relative_to(work_dir / "work").parts, text)
    if row is None:
        return f"the written ticket does not parse, so the store row of {task_id} is unchanged"
    if not any(r.get("task_id") == task_id for r in run_store.read_queue(runs_dir, initiative, kind="task")):
        return f"no store row for {task_id}"
    refused = run_store.upsert_row_detail(
        runs_dir, {"initiative": initiative, "task_id": task_id, "surfaces": row["surfaces"], "body": row["body"]}
    )
    return f"the store did not take the widened {task_id}: {refused}" if refused else ""


def apply_widen(work_dir: Path, action: Action, when: str, *, runs_dir: Path | None = None) -> tuple[bool, str]:
    """Edge. Never raises. `(True, reason)` naming the task and paths once the file is written; else `(False, note)`.

    With `runs_dir`, the store row takes the file's surfaces and body and the file is recorded as this workspace's own
    write. A store refusal rides in the reason and leaves the baseline unrecorded, so the export holds the file."""
    task_id, initiative = action.get("task_id", ""), action.get("initiative", "")
    paths = list(action.get("paths", []))
    path = _ticket_path(work_dir, initiative, task_id) if task_id and initiative else None
    if path is None:
        return False, "ticket not found"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"widen: cannot read {task_id}: {exc}"
    new_text, refusal = widen_ticket_text(text, list(zip(paths, action.get("additions", []))), when)
    if new_text is None:
        return False, refusal or "widen: rewrite refused"
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return False, f"widen: cannot write {task_id}: {exc}"
    reason = f"widened {task_id}: {', '.join(paths)}"
    if runs_dir is None:
        return True, reason
    refusal = _mirror_to_store(runs_dir, work_dir, path, initiative, task_id, new_text)
    if refusal:
        return True, f"{reason}; {refusal}"
    queue_export.record_written_paths(work_dir, [path])
    return True, reason
