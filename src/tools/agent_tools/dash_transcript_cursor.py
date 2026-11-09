"""Incremental transcript reads: an immutable cursor and a pure step that reads only what a file gained."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

KeyedTurn = tuple[str, dict]


@dataclass(frozen=True)
class PathCursor:
    """What was consumed of one file: its stat when read, the byte offset past the last complete line, and the keyed turns."""

    size: int
    mtime_ns: int
    offset: int
    turns: tuple[KeyedTurn, ...]


@dataclass(frozen=True)
class CursorState:
    """Per-path cursors in discovery order; `scope` names the host, chair session and prices the turns were built with."""

    paths: tuple[tuple[Path, PathCursor], ...] = ()
    scope: str = ""


def _first_per_key(items: Sequence[KeyedTurn]) -> tuple[KeyedTurn, ...]:
    first = dict(reversed([(key, i) for i, (key, _) in enumerate(items)]))
    return tuple(item for i, item in enumerate(items) if first[item[0]] == i)


def _advance_path(
    prev: PathCursor | None,
    stat: tuple[int, int],
    path: Path,
    read_from: Callable[[Path, int], bytes],
    parse: Callable[[Path, list[str]], Sequence[KeyedTurn]],
) -> PathCursor:
    """Unchanged stat keeps the cursor; growth reads past the offset; anything else re-parses from byte zero."""
    size, mtime_ns = stat
    if prev is not None and (prev.size, prev.mtime_ns) == stat:
        return prev
    base = prev if prev is not None and size > prev.size else PathCursor(0, 0, 0, ())
    chunk = read_from(path, base.offset)
    complete = chunk[: chunk.rfind(b"\n") + 1]
    lines = complete.decode("utf-8", errors="replace").splitlines()
    return PathCursor(size, mtime_ns, base.offset + len(complete), _first_per_key([*base.turns, *parse(path, lines)]))


def advance(
    previous: CursorState,
    stats: Mapping[Path, tuple[int, int]],
    read_from: Callable[[Path, int], bytes],
    parse: Callable[[Path, list[str]], Sequence[KeyedTurn]],
) -> CursorState:
    """The state after `stats` (path to (size, mtime_ns)); a path missing from `stats` drops out.

    A final line with no newline yet is left unread until its newline arrives.
    """
    old = dict(previous.paths)
    return replace(
        previous,
        paths=tuple((path, _advance_path(old.get(path), stat, path, read_from, parse)) for path, stat in stats.items()),
    )
