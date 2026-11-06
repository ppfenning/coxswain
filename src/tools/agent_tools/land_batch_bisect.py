"""Bisect a failing batch to find the failing sources.

A batch lands on one CI run. When it fails, the batch is halved and each half is tried again, so the sources that
pass still land and only the failing ones come back. Sources are opaque: nothing here reads their keys.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BisectResult[S]:
    """`landed` is in the order the sources landed; `returned` pairs each failing source with its failure detail."""

    landed: list[S]
    returned: list[tuple[S, str]]
    ci_runs: int


def _split[S](sources: Sequence[S]) -> tuple[Sequence[S], Sequence[S]]:
    """The left half takes (n + 1) // 2 items."""
    mid = (len(sources) + 1) // 2
    return sources[:mid], sources[mid:]


def _bisect[S](sources: Sequence[S], land: Callable[[list[S]], str | None]) -> BisectResult[S]:
    if not sources:
        return BisectResult([], [], 0)
    detail = land(list(sources))
    if detail is None:
        return BisectResult(list(sources), [], 1)
    if len(sources) == 1:
        return BisectResult([], [(sources[0], detail)], 1)
    left, right = _split(sources)
    first = _bisect(left, land)  # the right half lands on top of what the left half landed, so it runs second
    second = _bisect(right, land)
    return BisectResult(
        [*first.landed, *second.landed],
        [*first.returned, *second.returned],
        1 + first.ci_runs + second.ci_runs,
    )


def bisect_land[T](sources: Sequence[T], land: Callable[[list[T]], str | None]) -> BisectResult[T]:
    """Land `sources` with as few `land` calls as the rule allows; `land` returns None on success, else a detail."""
    return _bisect(sources, land)
