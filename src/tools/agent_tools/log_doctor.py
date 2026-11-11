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


def _read_text(path: Path) -> str:
    """Bytes that are not UTF-8 survive a round trip through surrogateescape."""
    try:
        return path.read_bytes().decode("utf-8", errors="surrogateescape")
    except OSError:
        return ""


def check_logs(work_dir: Path, runs_dir: Path) -> list[dict]:
    paths = _paths(work_dir, runs_dir)
    return log_rows({name: len(plan_fix(_read_text(path))[1]) for name, path in paths.items()})


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def _move_aside(path: Path, kept_text: str, damaged: tuple[str, ...]) -> None:
    """Appends the raw damaged lines to `<log>.damaged`, and only then replaces the log."""
    aside = path.with_name(path.name + ".damaged")
    with open(aside, "ab") as out:
        out.write(_encode("".join(line + "\n" for line in damaged)))
        out.flush()
        os.fsync(out.fileno())
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(_encode(kept_text))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def fix_logs(work_dir: Path, runs_dir: Path) -> dict[str, int]:
    """Returns the number of damaged lines moved per log. A log with no damage is not touched."""
    moved: dict[str, int] = {}
    for name, path in _paths(work_dir, runs_dir).items():
        kept_text, damaged = plan_fix(_read_text(path))
        if damaged:
            _move_aside(path, kept_text, damaged)
        moved[name] = len(damaged)
    return moved
