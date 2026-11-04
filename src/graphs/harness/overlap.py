"""Decide which phases may start together without touching the same files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

_NEW_MARKER = "(new)"
_MAX_REPORTED = 3


def _segments(path: str) -> tuple[str, ...]:
    bare = path.strip()
    bare = bare.removesuffix(_NEW_MARKER).strip().rstrip("/")
    return tuple(part for part in bare.split("/") if part)


def _collide(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


def shared_paths(a: Sequence[str], b: Sequence[str]) -> list[str]:
    """Sorted paths on which two surface lists collide; a prefix hit reports the longer path."""
    left = [_segments(p) for p in a]
    right = [_segments(p) for p in b]
    hits = {"/".join(max(x, y, key=len)) for x in left for y in right if _collide(x, y)}
    return sorted(hits)


def _reason(phase: str, other: str, paths: list[str]) -> str:
    return f"phase '{phase}' waits for phase '{other}': both declare {', '.join(paths[:_MAX_REPORTED])}"


def serialise(
    order: Sequence[str],
    surfaces: Mapping[str, Sequence[str]],
    running: Mapping[str, Sequence[str]],
) -> tuple[list[str], dict[str, str]]:
    """Split `order` into phases that may start now and held phases with a reason.

    Running phases are checked in name order, then earlier chosen phases in
    `order`. The first blocker wins. A held phase blocks nobody.
    """
    chosen: list[str] = []
    held: dict[str, str] = {}
    for phase in order:
        own = surfaces.get(phase, ())
        rivals = [(name, running[name]) for name in sorted(running)] + [
            (name, surfaces.get(name, ())) for name in chosen
        ]
        blockers = ((name, shared_paths(own, theirs)) for name, theirs in rivals if name != phase)
        first = next(((name, paths) for name, paths in blockers if paths), None)
        if first is None:
            chosen = [*chosen, phase]
        else:
            held = {**held, phase: _reason(phase, first[0], first[1])}
    return chosen, held
