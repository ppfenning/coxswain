"""The merge queue's settings and its open rule.

The profile carries a top-level `merge_queue:` mapping with `batch_size` (an int of at least 1, default 1) and `wait_s`
(a number of at least 0, default 0). A batch_size of 1 means the queue is off. The profile is read here, not through the
cartridges package, so the two validators stay independent. A malformed value falls back to its default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_BATCH_SIZE = 1
DEFAULT_WAIT_S = 0.0


@dataclass(frozen=True)
class MergeQueue:
    batch_size: int
    wait_s: float

    @property
    def enabled(self) -> bool:
        return self.batch_size >= 2


def _valid_batch_size(value: object) -> bool:
    """An int of at least 1; a bool is not an int here and a float is invalid."""
    return type(value) is int and value >= 1


def _valid_wait_s(value: object) -> bool:
    """An int or float of at least 0; a bool is not a number here. NaN fails the comparison, inf is a wait that never ends.

    The value is kept as given. Comparing a float against a huge int is exact in Python and never overflows.
    """
    return type(value) in (int, float) and value >= 0


def merge_queue_setting(profile: Mapping[str, Any] | None) -> MergeQueue:
    """The profile's `merge_queue` section; each key falls back to its default on its own."""
    section = profile.get("merge_queue") if isinstance(profile, Mapping) else None
    section = section if isinstance(section, Mapping) else {}
    batch_size, wait_s = section.get("batch_size"), section.get("wait_s")
    return MergeQueue(
        batch_size if _valid_batch_size(batch_size) else DEFAULT_BATCH_SIZE,
        wait_s if _valid_wait_s(wait_s) else DEFAULT_WAIT_S,
    )


def should_open(ready: int, elapsed_s: float, settings: MergeQueue) -> bool:
    """Open when the batch is full or the wait is over, never with nothing in hand (`ready` is 0)."""
    return ready >= 1 and (ready >= settings.batch_size or elapsed_s >= settings.wait_s)
