"""A queue directory as an intake source: drop a file, the harness picks it up.

    intake/<anything>.md            queued items, read in filename order
    intake/consumed/<anything>.md   the same files, moved here once decomposed

`manual` intake is a human typing `--idea` at a shell — honest, but it only
works while someone is there to type. A queue directory is the smallest real
alternative: any process (a cron job, a webhook receiver, a person's editor)
can drop a markdown file in `intake/`, and the harness runs decompose over it
on its own schedule.

Ordering is the filename's job, the same argument `workstore.phases` makes for
phase names: sorted by name, not by mtime, so the queue's order does not
depend on how the files got copied there. A sortable prefix (`001-`, a date)
is the writer's tool for expressing priority.

Consumption must be auditable — a queue that deletes its history cannot answer
"what did we decide about X" — so `consume` only ever moves a file into a
`consumed/` sibling directory. Nothing in this module deletes a file.

    read_queue(root) -> list[dict]      id, kind, title, body, path
    consume(path) -> Path               moved into <parent>/consumed/

Frontmatter is optional here, unlike `workstore`: a work item is a structured
thing the harness itself writes, but an intake item is a note someone jotted,
and a queue that rejects a bare note is a queue nobody uses. When frontmatter
is present it is parsed the same way `workstore` parses it — same fence, same
failure modes — because there is no reason for a second dialect of the same
idea.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from core.workstore import WorkStoreError, _split_frontmatter

__all__ = ["IntakeError", "read_queue", "consume"]

_FRONTMATTER = "---"


class IntakeError(Exception):
    """A queued item could not be read, or a consume could not complete safely."""


def _read_item(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeError(f"cannot read queued item {path}: {exc}") from exc

    lines = text.splitlines()
    if lines and lines[0].strip() == _FRONTMATTER:
        try:
            meta, body = _split_frontmatter(text, path)
        except WorkStoreError as exc:
            raise IntakeError(str(exc)) from exc
    else:
        meta, body = {}, text.strip()

    return {
        "id": path.stem,
        "kind": str(meta.get("kind") or "idea"),
        "title": str(meta.get("title") or path.stem),
        "body": body,
        "path": str(path),
    }


def read_queue(root: Path | str) -> list[dict[str, Any]]:
    """Every queued item, top-level only, in filename order. A missing queue is empty.

    Top-level only (no rglob): `consumed/` sits one level down specifically so
    a plain `*.md` glob already excludes it — an item stops being "queued" the
    moment it moves, with no separate filter to keep in sync.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    return [_read_item(path) for path in sorted(root.glob("*.md"))]


def consume(path: Path | str, *, consumed_dir: Path | str | None = None) -> Path:
    """Move a queued item into `consumed/`, preserving its filename. Never deletes.

    Refuses if the destination already exists: two runs both believing they
    own the same item is exactly the case a queue exists to prevent, and the
    second one has to find out rather than silently overwrite the first.
    """
    path = Path(path)
    destination_dir = Path(consumed_dir) if consumed_dir is not None else path.parent / "consumed"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / path.name
    if destination.exists():
        raise IntakeError(f"{destination} already exists; refusing to consume {path} twice")
    shutil.move(str(path), str(destination))
    return destination
