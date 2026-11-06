"""Stamp-keyed read cache the chair edge readers share across ticks.

The caller owns the cache object and passes it in. There is no module-level instance.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

Stamp = tuple[int, int]

DOC_VERSION = 1


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


def _encode_key(key: Any) -> dict[str, Any] | None:
    """Tagged form of a Path, a tuple of strings or a str. None for any other key."""
    if isinstance(key, Path):
        return {"t": "path", "v": str(key)}
    if isinstance(key, str):
        return {"t": "str", "v": key}
    if isinstance(key, tuple) and all(isinstance(part, str) for part in key):
        return {"t": "tuple", "v": list(key)}
    return None


def _decode_key(doc: Any) -> tuple[bool, Any]:
    """(True, key) for a well-formed tagged key, else (False, None)."""
    if not isinstance(doc, dict):
        return (False, None)
    tag, raw = doc.get("t"), doc.get("v")
    if tag == "path" and isinstance(raw, str):
        return (True, Path(raw))
    if tag == "str" and isinstance(raw, str):
        return (True, raw)
    if tag == "tuple" and isinstance(raw, list) and all(isinstance(p, str) for p in raw):
        return (True, tuple(raw))
    return (False, None)


def _survives_json(value: Any) -> bool:
    try:
        return json.loads(json.dumps(value)) == value
    except TypeError, ValueError:
        return False


def _decode_entry(entry: Any) -> tuple[Any, tuple[Stamp, Any]] | None:
    """(key, (stamp, value)) for a well-formed entry, else None."""
    if not isinstance(entry, dict) or "value" not in entry:
        return None
    ok, key = _decode_key(entry.get("key"))
    stamp = entry.get("stamp")
    is_stamp = (
        isinstance(stamp, list)
        and len(stamp) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) for n in stamp)
    )
    if not ok or not is_stamp:
        return None
    return (key, ((stamp[0], stamp[1]), entry["value"]))


def dump_entries(cache: StampCache) -> dict[str, Any]:
    """Oldest-first document of the entries whose key and value survive a JSON round trip."""
    encoded = [(_encode_key(key), stamp, value) for key, (stamp, value) in cache._entries.items()]
    return {
        "version": DOC_VERSION,
        "entries": [
            {"key": k, "stamp": list(stamp), "value": value}
            for k, stamp, value in encoded
            if k is not None and _survives_json(value)
        ],
    }


def load_entries(cache: StampCache, doc: Any) -> int:
    """Insert the decodable entries of doc, newest max_entries at most. Returns the count loaded."""
    if not isinstance(doc, dict) or doc.get("version") != DOC_VERSION:
        return 0
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return 0
    decoded = [d for d in map(_decode_entry, entries) if d is not None]
    # Same-module access to the cache's own store; the public API has no insert without a read.
    wanted = list(dict(decoded).items())[-cache.max_entries :]
    for key, stored in wanted:
        cache._entries[key] = stored
        cache._entries.move_to_end(key)
    while len(cache._entries) > cache.max_entries:
        cache._entries.popitem(last=False)
    return len(wanted)


def save_cache(cache: StampCache, path: str | os.PathLike[str]) -> None:
    """Write the cache to path through a temp file in the same directory. Never raises."""
    target = Path(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dump_entries(cache), handle)
        os.replace(tmp, target)
    except OSError, TypeError, ValueError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def load_cache(cache: StampCache, path: str | os.PathLike[str]) -> int:
    """Load a saved cache file into cache. 0 for a missing, unreadable or malformed file."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return 0
    return load_entries(cache, doc)
