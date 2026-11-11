"""Restore a ready ticket to `approved` with a dated note; the store mirror belongs to the executor.

The inverse of chair_apply_release for a ticket the approval hold released wrongly: `ready` is restorable,
`approved` is a no-op, any other state is refused.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

__all__ = ["apply_restore", "restore_ticket"]

_STATE = re.compile(r"""^state:[ \t]*(?P<q>['"]?)(?P<value>[\w-]+)(?P=q)(?P<tail>[ \t]+#.*)?[ \t]*$""")


def _split(text: str) -> tuple[list[str], str] | None:
    """The header lines (opening fence included) and the body; `None` without a closed header."""
    close = text.find("\n---\n", 3) if text.startswith("---\n") else -1
    return None if close < 0 else (text[:close].split("\n"), text[close + len("\n---\n"):])


def restore_ticket(text: str, reason: str, day: str) -> tuple[str | None, str | None]:
    """`(new_text, None)` with state approved and one dated note; `(text, None)` when already approved; else `(None, note)`.

    The reason lands only in the body, so a colon in it cannot break the frontmatter."""
    parts = _split(text)
    if parts is None:
        return None, "restore: work item has no frontmatter"
    header, body = parts
    found = next(((i, m) for i, line in enumerate(header) if (m := _STATE.match(line))), None)
    if found is None:
        return None, "restore: work item has no state field"
    at, match = found
    state = match["value"]
    if state == "approved":
        return text, None
    if state != "ready":
        return None, f"restore: work item state is {state!r}, not restoring"
    why = reason.strip().rstrip(".").rstrip()
    if not why or not day.strip() or any(c in why + day for c in "\r\n"):
        return None, "restore: reason and day must be non-empty single lines"
    note = f"Restored to approved on {day.strip()}: {why}."
    approved = header[:at] + [f"state: approved{match['tail'] or ''}"] + header[at + 1:]
    new_body = (body.rstrip("\n") + "\n\n" if body.strip() else "") + note + "\n"
    return "\n".join(approved) + "\n---\n" + new_body, None


def _ticket_path(work_dir: Path, initiative: str, task_id: str) -> Path | None:
    """Edge. The one ticket file under `work/<initiative>/**/<task_id>.md`; None when none matches."""
    return next(iter(sorted((work_dir / "work" / initiative).glob(f"**/{task_id}.md"))), None)


def apply_restore(work_dir: Path, initiative: str, task_ids: Sequence[str], reason: str, day: str) -> tuple[bool, str]:
    """Edge. Never raises. Every ticket is checked before any is written, so a refusal leaves all files as they were."""
    ids = list(dict.fromkeys(task_ids))
    if not ids or not initiative:
        return False, "restore: no ticket named"
    planned: list[tuple[Path, str]] = []
    for task_id in ids:
        path = _ticket_path(work_dir, initiative, task_id)
        if path is None:
            return False, "ticket not found"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"restore: cannot read {task_id}: {exc}"
        new_text, refusal = restore_ticket(text, reason, day)
        if new_text is None:
            return False, f"{refusal} ({task_id})"
        planned.append((path, new_text))
    for (path, new_text), task_id in zip(planned, ids):
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return False, f"restore: cannot write {task_id}: {exc}"
    return True, f"restored {', '.join(ids)}"
