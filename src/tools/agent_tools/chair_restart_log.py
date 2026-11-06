"""The chair's durable restart record and the note it adds to the tick line: a pure core and a thin file edge."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

__all__ = ["RestartRecord", "bump", "read_record", "record_path", "restart_note", "with_note", "write_record"]

_WINDOW = timedelta(hours=24)
# `_chair_once_exit` in cli.py reads this substring as a failed tick, so a note must never carry it.
_TICK_ERROR = "| tick error: "


@dataclass(frozen=True)
class RestartRecord:
    times: tuple[str, ...]  # ISO restart stamps, tz-aware, oldest first
    reason: str


def _recent(times: tuple[str, ...], now: datetime) -> tuple[str, ...]:
    """The stamps no more than 24h before `now`; an unparseable or naive stamp is dropped."""

    def parsed(text: str) -> datetime | None:
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return None
        return stamp if stamp.tzinfo is not None else None

    return tuple(t for t in times if (s := parsed(t)) is not None and now - s <= _WINDOW)


def bump(prev: RestartRecord | None, reason: str, now: datetime) -> RestartRecord:
    """Append `now` to the restart times, drop those older than 24h and keep `reason`. `now` is tz-aware."""
    earlier = prev.times if prev else ()
    return RestartRecord(times=_recent((*earlier, now.isoformat()), now), reason=reason)


def restart_note(record: RestartRecord | None, now: datetime) -> str:
    """`restarts N in 24h, last: <reason>`; empty when there is no restart inside 24h."""
    count = len(_recent(record.times, now)) if record else 0
    if record is None or count == 0:
        return ""
    return f"restarts {count} in 24h, last: {' '.join(record.reason.split())}"


def with_note(line: str, note: str) -> str:
    """`line | note`, so a `chair MM-DD HH:MM TZ` line still parses. An empty note or one holding a tick-error marker leaves `line` alone."""
    if not note or _TICK_ERROR in note:
        return line
    return f"{line} | {note}"


def record_path(runs_dir: Path) -> Path:
    return runs_dir / "chair-restarts.json"


def read_record(path: Path) -> RestartRecord | None:
    """Edge. The record at `path`; None when missing, unreadable or malformed."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    times = doc.get("times") if isinstance(doc, dict) else None
    reason = doc.get("reason") if isinstance(doc, dict) else None
    if not isinstance(times, list) or not all(isinstance(t, str) for t in times) or not isinstance(reason, str):
        return None
    return RestartRecord(times=tuple(times), reason=reason)


def write_record(path: Path, record: RestartRecord) -> None:
    """Edge. Write to a temp file beside `path`, then rename it over `path`."""
    body = json.dumps({"times": list(record.times), "reason": record.reason})
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
