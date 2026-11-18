"""Cross-tick cache of the chair's store readers, keyed by the store's change counters (H6).

Each tick reads one row per table from `pg_stat_user_tables` (`run_store.table_counters`): the rows inserted, updated
and deleted since statistics began. A reader names the tables it reads; while their counters stand, the tick reuses the
rows the reader returned last time and sends no query for them. One write to one table re-reads exactly the readers
that name it.

The counters are cumulative statistics, flushed by the writing backend when it goes idle or exits, so they can trail a
committed write by about a second, and up to about a minute on a busy server (see `run_store.store_change_stamp`). Every
entry is also re-read after `MAX_AGE_S` whatever the counters say, which bounds what a missed count (TRUNCATE, a read
replica, `track_counts = off`) can hide. Unknown counters (no Postgres store, or the read failed) cache nothing.

The decisions are pure functions; `ReaderCache` is the caller-owned state, with the clock passed in.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Callable, Collection, Hashable, Mapping
from dataclasses import dataclass
from typing import cast

__all__ = ["MAX_AGE_S", "ReaderCache", "TableCounters", "is_fresh", "store_note", "table_stamp"]

TableCounters = Mapping[str, int]
Stamp = tuple[tuple[str, int], ...]

MAX_AGE_S = 600.0  # a reader is re-read at least this often, whatever the counters say


def table_stamp(tables: Collection[str], counters: TableCounters | None) -> Stamp | None:
    """Pure. The counters of `tables`, in name order; a table the store does not list reads -1. None when the counters
    are unknown, which caches nothing."""
    if counters is None:
        return None
    return tuple((table, int(counters.get(table, -1))) for table in sorted(set(tables)))


def is_fresh(stored: Stamp | None, stored_at: float, stamp: Stamp | None, now: float, max_age_s: float = MAX_AGE_S) -> bool:
    """Pure. Whether an entry read at `stored_at` under `stored` still stands at `now` under `stamp`: both known, equal,
    and younger than `max_age_s`."""
    return stored is not None and stamp is not None and stored == stamp and 0 <= now - stored_at < max_age_s


@dataclass
class _Entry:
    stamp: Stamp
    at: float
    value: object


class ReaderCache:
    """Reader name (and key) to the value it last returned, with the stamp and the time it was read under.

    `full_reads` and `reuses` count, per reader name, the reads that went to the store and the ones that did not; the
    chair's timing line prints them, and the smoke suite reads them."""

    def __init__(self, clock: Callable[[], float], max_age_s: float = MAX_AGE_S) -> None:
        self._clock = clock
        self._max_age_s = max_age_s
        self._entries: dict[tuple[str, Hashable], _Entry] = {}
        self.full_reads: Counter[str] = Counter()
        self.reuses: Counter[str] = Counter()

    def get[T](
        self, name: str, tables: Collection[str], counters: TableCounters | None, read: Callable[[], T], key: Hashable = ()
    ) -> T:
        """`read()`'s value, reused while the counters of `tables` stand. A read that raises stores nothing."""
        stamp, now = table_stamp(tables, counters), self._clock()
        entry = self._entries.get((name, key))
        if entry is not None and is_fresh(entry.stamp, entry.at, stamp, now, self._max_age_s):
            self.reuses[name] += 1
            return cast(T, copy.deepcopy(entry.value))  # the name keys one reader, so its entry holds that reader's T; a copy, as a fresh read is: a caller that edits its rows edits only its own
        self.full_reads[name] += 1
        value = read()
        if stamp is None:
            self._entries.pop((name, key), None)
        else:
            # One entry per name: a new key (another initiative set, say) replaces the old one, so the cache stays small.
            for old in [k for k in self._entries if k[0] == name and k != (name, key)]:
                del self._entries[old]
            self._entries[(name, key)] = _Entry(stamp, now, copy.deepcopy(value))  # the caller's value stays the caller's
        return value

    def tick_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """(full reads, reuses) by reader name so far; the caller diffs two of them for one tick."""
        return dict(self.full_reads), dict(self.reuses)


def store_note(before: tuple[Mapping[str, int], Mapping[str, int]], after: tuple[Mapping[str, int], Mapping[str, int]]) -> str:
    """Pure. The timing line's `store:` part for one tick: the readers that went to the store, by name, and how many
    were reused. `store: read=none reused=0` when no stamped reader ran."""
    read = sorted(name for name, n in after[0].items() if n > before[0].get(name, 0))
    reused = sum(n - before[1].get(name, 0) for name, n in after[1].items())
    return f"store: read={','.join(read) or 'none'} reused={reused}"
