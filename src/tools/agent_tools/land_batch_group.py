"""Group land sources into batches whose file sets are disjoint.

A source is a mapping with keys initiative, phase, run, repo, files and tasks. The rule is pure: it reads nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from functools import reduce
from typing import Any

Source = Mapping[str, Any]


def overlaps(a: Source, b: Source) -> list[str]:
    return sorted(set(a["files"]) & set(b["files"]))


def batch_key(sources: Sequence[Source]) -> str:
    """First 10 hex characters of the sha1 of the sorted initiative/phase names joined by a comma."""
    names = ",".join(sorted(f"{s['initiative']}/{s['phase']}" for s in sources))
    return hashlib.sha1(names.encode()).hexdigest()[:10]


def _fits(batch: Sequence[Source], source: Source) -> bool:
    """An empty files list is unknown, so it fits nowhere and admits nothing."""
    if not source["files"]:
        return False
    return all(
        m["files"]
        and m["repo"] == source["repo"]
        and m["initiative"] != source["initiative"]
        and not overlaps(m, source)
        for m in batch
    )


def disjoint_batches(sources: Sequence[Source], size: int) -> list[list[Source]]:
    """Greedy and order-keeping: each source joins the earliest batch with room that it fits."""
    cap = max(size, 1)

    def place(batches: list[list[Source]], source: Source) -> list[list[Source]]:
        index = next(
            (i for i, b in enumerate(batches) if len(b) < cap and _fits(b, source)),
            None,
        )
        if index is None:
            return [*batches, [source]]
        return [*batches[:index], [*batches[index], source], *batches[index + 1 :]]

    return reduce(place, sources, [])
