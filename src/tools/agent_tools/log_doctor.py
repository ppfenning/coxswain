"""Doctor rows and a fix for damaged lines in the append-only logs.

The core (`plan_fix`, `log_rows`) is pure. `check_logs` and `fix_logs` are the edge.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from agent_tools.jsonl_log import parse_log

__all__ = ["LOG_NAMES", "check_logs", "fix_logs", "log_rows", "plan_fix"]

# courier.jsonl lives in the work dir; the other two live in the runs dir.
_WORK_LOGS = ("courier.jsonl",)
_RUNS_LOGS = ("land.jsonl", "chair.actions.jsonl")
LOG_NAMES = _WORK_LOGS + _RUNS_LOGS

_NUL = "\x00"


def _is_damaged(raw: str) -> bool:
    """A NUL byte marks a line damaged even when parse_log would accept it stripped."""
    return _NUL in raw or bool(parse_log(raw).damaged)


def _stripped(raw: str) -> str | None:
    """The line without NULs and edge whitespace, or None when it still is not a row."""
    candidate = raw.replace(_NUL, "").strip()
    return candidate if parse_log(candidate).rows else None


def _keep(line: str) -> str | None:
    raw = line.removesuffix("\r")
    return _stripped(raw) if _is_damaged(raw) else line


def plan_fix(text: str) -> tuple[str, tuple[str, ...]]:
    """Returns (kept_text, damaged raw lines). Clean text comes back unchanged."""
    pieces = text.split("\n")
    raws = tuple(piece.removesuffix("\r") for piece in pieces)
    damaged = tuple(raw for raw in raws if _is_damaged(raw))
    if not damaged:
        return (text, ())
    lines = pieces[:-1] if pieces[-1] == "" else pieces
    kept = tuple(_keep(line) for line in lines)
    return ("".join(f"{line}\n" for line in kept if line is not None), damaged)


def log_rows(counts: Mapping[str, int]) -> list[dict]:
    """One row per log. A damaged log is a warning, so `ok` stays True."""
    return [_row(name, counts.get(name, 0)) for name in LOG_NAMES]


def _row(name: str, count: int) -> dict:
    check = f"log {name}"
    if count == 0:
        return {"check": check, "ok": True, "detail": "no damaged lines"}
    noun = "line" if count == 1 else "lines"
    return {"check": check, "ok": True, "warn": True, "detail": f"{count} damaged {noun}; run cox setup doctor --fix"}


def _paths(work_dir: Path, runs_dir: Path) -> dict[str, Path]:
    return {name: (work_dir if name in _WORK_LOGS else runs_dir) / name for name in LOG_NAMES}


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _decode(data: bytes) -> str:
    """surrogateescape, so a good line with invalid UTF-8 is written back byte for byte.

    read_log decodes with errors="replace". Both turn an invalid byte into one non-ASCII
    character, so a line is damaged under both decoders or under neither.
    """
    return data.decode("utf-8", errors="surrogateescape")


def check_logs(work_dir: Path, runs_dir: Path) -> list[dict]:
    """Counts with plan_fix, not read_log: read_log strips NULs and would call a NUL line clean."""
    counts = {name: len(plan_fix(_decode(_read_bytes(path)))[1]) for name, path in _paths(work_dir, runs_dir).items()}
    return log_rows(counts)


def _write_all(fd: int, payload: bytes) -> None:
    """os.write may store fewer bytes than asked, so loop; a write of zero bytes is a full disk."""
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError(errno.ENOSPC, "wrote no bytes to the .damaged file")
        view = view[written:]


def _append_damaged(path: Path, damaged: tuple[str, ...]) -> None:
    """A crash between this and the replace leaves the lines in both files, and a rerun appends them
    again. That duplicates a damaged line and never loses one."""
    payload = "".join(f"{raw}\n" for raw in damaged).encode("utf-8", errors="surrogateescape")
    fd = os.open(path.with_name(path.name + ".damaged"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace(path: Path, kept: str, expected: bytes) -> None:
    """Swaps in a synced temp file that keeps the log's mode and owner.

    Refuses when the log changed since it was read, so a line a live writer appended in between
    is not dropped. There is no lock, so a write landing between that check and the rename is
    still lost: run the fix while the courier and land processes are idle.
    """
    temp = path.with_name(path.name + ".fix.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(kept.encode("utf-8", errors="surrogateescape"))
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, temp)
        with contextlib.suppress(OSError):
            stat = path.stat()
            shutil.chown(temp, user=stat.st_uid, group=stat.st_gid)
        if path.read_bytes() != expected:
            raise OSError(errno.EBUSY, f"{path.name} changed while it was being fixed; run the fix again")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def fix_logs(work_dir: Path, runs_dir: Path) -> dict[str, int]:
    """Moves damaged lines to `<log>.damaged` first, then rewrites the log; returns the count moved per log."""
    moved: dict[str, int] = {}
    for name, path in _paths(work_dir, runs_dir).items():
        data = _read_bytes(path)
        kept, damaged = plan_fix(_decode(data))
        if damaged:
            _append_damaged(path, damaged)
            _replace(path, kept, data)
        moved[name] = len(damaged)
    return moved
