"""Doctor rows and a fix for damaged lines in the three append-only logs."""

import os
from collections.abc import Mapping
from pathlib import Path

from agent_tools.jsonl_log import parse_log

COURIER = "courier.jsonl"
LAND = "land.jsonl"
CHAIR_ACTIONS = "chair.actions.jsonl"
LOG_NAMES = (COURIER, LAND, CHAIR_ACTIONS)


def _is_row(line: str) -> bool:
    return len(parse_log(line).rows) == 1


def _classify(piece: str) -> tuple[str | None, str | None]:
    """Returns (kept, damaged) for one piece of text split on newline; each is None when it does not apply."""
    raw = piece.removesuffix("\r")
    if "\x00" in raw:
        stripped = raw.replace("\x00", "")
        return (stripped if _is_row(stripped) else None, raw)
    if parse_log(raw).skipped:
        return (None, raw)
    return (piece, None)


def plan_fix(text: str) -> tuple[str, tuple[str, ...]]:
    """Returns (kept_text, damaged_raw_lines). A NUL line that parses stripped stays in kept_text, stripped."""
    pieces = tuple(_classify(piece) for piece in text.split("\n"))
    damaged = tuple(raw for _, raw in pieces if raw is not None)
    if not damaged:
        return (text, ())
    kept = tuple(line for line, _ in pieces if line is not None)
    closed = kept if not kept or kept[-1] == "" else (*kept, "")
    return ("\n".join(closed), damaged)


def log_rows(counts: Mapping[str, int]) -> list[dict]:
    return [
        {"check": name, "ok": True, "detail": "ok"}
        if count == 0
        else {
            "check": name,
            "ok": True,
            "warn": True,
            "detail": f"{count} damaged line(s): run cox setup doctor --fix",
        }
        for name, count in counts.items()
    ]


def _paths(work_dir: Path, runs_dir: Path) -> dict[str, Path]:
    return {COURIER: work_dir / COURIER, LAND: runs_dir / LAND, CHAIR_ACTIONS: runs_dir / CHAIR_ACTIONS}


def _read_raw(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _decode(raw: bytes) -> str:
    """Bytes that are not UTF-8 survive a round trip through surrogateescape."""
    return raw.decode("utf-8", errors="surrogateescape")


def _read_text(path: Path) -> str:
    return _decode(_read_raw(path))


def check_logs(work_dir: Path, runs_dir: Path) -> list[dict]:
    paths = _paths(work_dir, runs_dir)
    return log_rows({name: len(plan_fix(_read_text(path))[1]) for name, path in paths.items()})


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


# Writers append to these logs without a lock, so a line can land between the read and the replace.
_SETTLE_TRIES = 5


def _fix_one(path: Path) -> int:
    """Moves the damaged lines of one log aside; the number moved.

    The cleaned copy is written first. Before it replaces the log, the log is read again: a tail appended since is
    sorted the same way and carried into the copy, until the log holds still. A log that was rewritten rather than
    appended to, or that keeps growing, is left as it is for the next fix. The raw damaged lines are appended to
    `<log>.damaged` before the replace."""
    seen = _read_raw(path)
    kept_text, damaged = plan_fix(_decode(seen))
    if not damaged:
        return 0
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(_encode(kept_text))
        for _ in range(_SETTLE_TRIES):
            now = _read_raw(path)
            if now == seen:
                break
            if not now.startswith(seen):
                tmp.unlink(missing_ok=True)
                return 0
            tail_kept, tail_damaged = plan_fix(_decode(now[len(seen) :]))
            with open(tmp, "ab") as out:
                out.write(_encode(tail_kept))
            damaged, seen = (*damaged, *tail_damaged), now
        else:
            tmp.unlink(missing_ok=True)
            return 0
        aside = path.with_name(path.name + ".damaged")
        with open(aside, "ab") as out:
            out.write(_encode("".join(line + "\n" for line in damaged)))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return len(damaged)


def fix_logs(work_dir: Path, runs_dir: Path) -> dict[str, int]:
    """Returns the number of damaged lines moved per log. A log with no damage is not touched."""
    return {name: _fix_one(path) for name, path in _paths(work_dir, runs_dir).items()}
