"""Stamp-keyed read cache the chair edge readers share across ticks.

The caller owns the cache object and passes it in. There is no module-level instance.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

Stamp = tuple[int, int]


def file_stamp(path: str | os.PathLike[str]) -> Stamp | None:
    """(st_mtime_ns, st_size) for a path, or None when it is missing or unreadable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class StampCache:
    """Maps a key to (stamp, value). Holds at most max_entries, evicting the least recently used."""

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self._entries: OrderedDict[Any, tuple[Stamp, Any]] = OrderedDict()

    @property
    def reads(self) -> tuple[int, int]:
        """(hits, misses)."""
        return (self.hits, self.misses)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: Any, stamp: Stamp | None, read: Callable[[], Any]) -> Any:
        """The stored value when its stamp equals stamp, else read(). A None stamp is never stored."""
        stored = self._entries.get(key)
        if stamp is not None and stored is not None and stored[0] == stamp:
            self.hits += 1
            self._entries.move_to_end(key)
            return stored[1]
        self.misses += 1
        value = read()
        if stamp is None:
            self._entries.pop(key, None)
            return value
        self._entries[key] = (stamp, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return value

    def keep(self, keys: Iterable[Any]) -> None:
        """Drop every entry whose key is not in keys."""
        wanted = set(keys)
        for key in [k for k in self._entries if k not in wanted]:
            del self._entries[key]
