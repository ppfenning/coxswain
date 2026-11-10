"""Tolerant JSONL reading and single-write appending, shared by every log reader."""

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParsedLog:
    """`damaged` holds the raw lines that failed to parse, NUL bytes intact."""

    rows: tuple[dict, ...]
    skipped: int
    damaged: tuple[str, ...]


def _classify(raw: str) -> tuple[str, dict | None]:
    """Returns ("row", dict), ("blank", None) or ("damaged", None) for one raw line."""
    cleaned = raw.replace("\x00", "")
    if cleaned == raw and not raw.strip():
        return ("blank", None)
    try:
        value = json.loads(cleaned)
    except ValueError, RecursionError:
        return ("damaged", None)
    if isinstance(value, dict):
        return ("row", value)
    return ("damaged", None)


def parse_log(text: str) -> ParsedLog:
    """Lines end at "\\n"; a trailing "\\r" is dropped. Never raises."""
    raws = tuple(piece.removesuffix("\r") for piece in text.split("\n"))
    classified = tuple((raw, *_classify(raw)) for raw in raws)
    damaged = tuple(raw for raw, kind, _ in classified if kind == "damaged")
    return ParsedLog(
        rows=tuple(row for _, kind, row in classified if row is not None and kind == "row"),
        skipped=len(damaged),
        damaged=damaged,
    )


def read_log(path: Path) -> ParsedLog:
    try:
        data = path.read_bytes()
    except OSError:
        return ParsedLog((), 0, ())
    return parse_log(data.decode("utf-8", errors="replace"))


def append_line(path: Path, entry: dict, *, sync: bool) -> None:
    """One os.write per call; a torn tail (last byte not a newline) is closed in that same write."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        torn = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        payload = (("\n" if torn else "") + json.dumps(entry) + "\n").encode("utf-8")
        os.write(fd, payload)
        if sync:
            os.fsync(fd)
    finally:
        os.close(fd)
