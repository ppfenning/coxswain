"""Edge reader for the decompose-miss backoff: an intake's fingerprint, the value that changes when the intake does.

A miss counts up only while the fingerprint stays the same, so it covers each thing whose change can fix a launch
that found no idea file: the path itself, the file at that path (absent, or its text) and the intake's store row
(a hand edit, `cox route ready`, a new recorded path).
"""
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

Row = Mapping[str, object]


def _own_row(rows: Sequence[Row], path: str) -> Row | None:
    """The intake row whose key is the path's file stem, as `chair_facts.intake_path_of_row` keys it."""
    stem = Path(path).stem
    return next((r for r in rows if r.get("kind") == "intake" and str(r.get("task_id")) == stem), None)


def fingerprint_of(path: str, text: str | None, row: Row | None) -> str:
    """Pure. A short digest of the path, the file text (None when there is no file) and the row."""
    payload = json.dumps([path, text, dict(row) if row is not None else None], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def intake_fingerprint(ws: Path, rows: Sequence[Row], path: str) -> str:
    """Edge. The fingerprint of the intake at `path` (workspace-relative) as it is now."""
    try:
        text: str | None = (Path(ws) / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    return fingerprint_of(path, text, _own_row(rows, path))
