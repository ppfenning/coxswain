"""Release an approved ticket back to `ready` with a dated note; the state mirror belongs to the executor.

With `runs_dir`, the note also reaches the store row's body and the workspace's export baseline, so the next export
finds the file and the row agreeing instead of holding the note as a hand edit.

Ticket frontmatter carries no verdict field, so "approved or done-with-verdict" is read from `state`:
`approved` and `done` are releasable, `ready` is a no-op, any other state is refused.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from agent_tools import queue_export, queue_rows, route_edit, run_store

__all__ = ["apply_release", "release_ticket"]

_STATE = re.compile(r"""^state:[ \t]*(?P<q>['"]?)(?P<value>[\w-]+)(?P=q)(?P<tail>[ \t]+#.*)?[ \t]*$""")
_RELEASABLE = ("approved", "done")


def _split(text: str) -> tuple[list[str], str] | None:
    """The header lines (opening fence included) and the body; `None` without a closed header."""
    close = text.find("\n---\n", 3) if text.startswith("---\n") else -1
    return None if close < 0 else (text[:close].split("\n"), text[close + len("\n---\n"):])


def _why(reason: str) -> str:
    return reason.strip().rstrip(".").rstrip()


def _note(reason: str, day: str) -> str:
    """The one paragraph a release appends, for the file and the store row alike."""
    return f"Released from approval on {day.strip()}: {_why(reason)}. Rebuild on the current main."


def release_ticket(text: str, reason: str, day: str) -> tuple[str | None, str | None]:
    """`(new_text, None)` with state ready and one dated note; `(text, None)` when already ready; else `(None, note)`.

    The reason lands only in the body, so a colon in it cannot break the frontmatter. The note changes the body hash."""
    parts = _split(text)
    if parts is None:
        return None, "release: work item has no frontmatter"
    header, body = parts
    found = next(((i, m) for i, line in enumerate(header) if (m := _STATE.match(line))), None)
    if found is None:
        return None, "release: work item has no state field"
    at, match = found
    state = match["value"]
    if state == "ready":
        return text, None
    if state not in _RELEASABLE:
        return None, f"release: work item state is {state!r}, not releasing"
    why = _why(reason)
    if not why or not day.strip() or any(c in why + day for c in "\r\n"):
        return None, "release: reason and day must be non-empty single lines"
    note = _note(reason, day)
    readied = header[:at] + [f"state: ready{match['tail'] or ''}"] + header[at + 1:]
    new_body = (body.rstrip("\n") + "\n\n" if body.strip() else "") + note + "\n"
    return "\n".join(readied) + "\n---\n" + new_body, None


def _ticket_path(work_dir: Path, initiative: str, task_id: str) -> Path | None:
    """Edge. The one ticket file under `work/<initiative>/**/<task_id>.md`; None when none matches."""
    return next(iter(sorted((work_dir / "work" / initiative).glob(f"**/{task_id}.md"))), None)


def _release_row(stored: dict, note: str) -> dict:
    """The partial row that gives the stored task `note` as the last paragraph of its body, once; never carries `state`."""
    return {"initiative": stored["initiative"], "task_id": stored["task_id"], "body": route_edit.append_note(stored.get("body") or "", note)}


def _mirror_note(runs_dir: Path, work_dir: Path, path: Path, new_text: str, note: str) -> str:
    """Edge. "" only when the store row took `note`; else why not. Never raises."""
    try:
        parts = path.relative_to(work_dir / "work").parts
    except ValueError:
        return "ticket is outside work/"
    parsed = queue_rows.parse_item("task", parts, new_text)
    if parsed is None:
        return "ticket text does not parse"
    stored = next((r for r in run_store.read_queue(runs_dir, parsed["initiative"], kind="task") if r.get("task_id") == parsed["task_id"]), None)
    if stored is None:
        return "no store row"
    return run_store.upsert_row_detail(runs_dir, _release_row(stored, note))


def apply_release(
    work_dir: Path, initiative: str, task_ids: Sequence[str], reason: str, day: str, *, runs_dir: Path | None = None
) -> tuple[bool, str]:
    """Edge. Never raises. Every ticket is checked before any is written, so a refusal leaves all files as they were.

    With `runs_dir`, each ticket whose text changed also has its note upserted to the store row, and only when the
    store took it is the file recorded as a baseline of the workspace (the parent of `runs_dir`): a baseline without
    the note in the store would let the next export rewrite the note away. A refusal is reported in the message."""
    ids = list(dict.fromkeys(task_ids))
    if not ids or not initiative:
        return False, "release: no ticket named"
    planned: list[tuple[Path, str, bool]] = []
    for task_id in ids:
        path = _ticket_path(work_dir, initiative, task_id)
        if path is None:
            return False, "ticket not found"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"release: cannot read {task_id}: {exc}"
        new_text, refusal = release_ticket(text, reason, day)
        if new_text is None:
            return False, f"{refusal} ({task_id})"
        planned.append((path, new_text, new_text != text))
    warnings: list[str] = []
    for (path, new_text, changed), task_id in zip(planned, ids):
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return False, f"release: cannot write {task_id}: {exc}"
        if runs_dir is not None and changed:
            if refused := _mirror_note(runs_dir, work_dir, path, new_text, _note(reason, day)):
                warnings.append(f"the store did not take the note of {task_id}: {refused}; the file is not baselined")
            else:
                queue_export.record_written_paths(runs_dir.parent, [path])
    return True, "; ".join([f"released {', '.join(ids)}", *warnings])
