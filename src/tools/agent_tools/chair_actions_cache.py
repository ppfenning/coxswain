"""The chair's `chair_actions` window, kept across ticks and topped up from its newest rows (H6).

`chair_read_stale.read_chair_actions` reads the whole window every tick: about 180,000 rows and 3 s at the chair
machine's volume, nearly all of it unchanged since the last tick. The window is kept instead, and each later tick sends
two small queries: how many rows the window holds now, and the rows at or after a bound a little before the newest kept
`ts`. The new rows are added by primary key; then, if the kept rows number what the store counts, they are the window.

That rests on `chair_actions` being insert-only: `store_cli record-action` inserts and skips a key already there, and
nothing updates or deletes a row. Kept rows that are all still in the store and as many as the store counts are exactly
the store's rows. Any other shape reads the whole window again, as every tick did before: a count that does not match (a
row written with a `ts` older than the bound, a row deleted by hand), a new window date, an unreadable `ts`, or a failed
query.

The change counters are not used: they trail a write by up to a minute, and the tick after a write must see it.

The decisions are pure functions; `ActionsCache` is the caller-owned state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import chair_read_stale, run_store
from agent_tools.chair_action_rows import FrozenRow, freeze
from agent_tools.chair_read_stale import actions_cutoff, count_and_tail_on

__all__ = ["ROW_KEY", "TAIL_MARGIN", "ActionsCache", "Kept", "kept_from", "tail_bound", "topped_up"]

ROW_KEY = ("ts", "holder", "kind", "target")  # the table's primary key
# A row is written a moment after the `ts` it carries; one written later than this behind the newest kept row is not in
# the tail, its count does not match, and the whole window is read again.
TAIL_MARGIN = timedelta(minutes=15)

Key = tuple[str, ...]


@dataclass(frozen=True)
class Kept:
    """The window opening on `cutoff`: its rows in the order they were read, their keys, and the newest `ts` text."""

    cutoff: str
    rows: tuple[FrozenRow, ...]
    keys: frozenset[Key]
    newest: str


def _key(row: Mapping[str, object]) -> Key:
    return tuple(str(row.get(column)) for column in ROW_KEY)


def _ts(row: Mapping[str, object]) -> str:
    ts = row.get("ts")
    return ts.isoformat() if isinstance(ts, datetime) else str(ts or "")


def kept_from(cutoff: str, rows: Iterable[Mapping[str, object]]) -> Kept:
    """Pure. A full read of the window opening on `cutoff`, kept."""
    frozen = tuple(map(freeze, rows))
    return Kept(cutoff, frozen, frozenset(map(_key, frozen)), max(map(_ts, frozen), default=""))


def topped_up(kept: Kept, tail: Iterable[Mapping[str, object]]) -> Kept:
    """Pure. `kept` with each `tail` row whose key it does not hold added after it, in tail order."""
    new: list[FrozenRow] = []
    keys = set(kept.keys)
    for row in tail:
        key = _key(row)
        if key not in keys:
            keys.add(key)
            new.append(freeze(row))
    if not new:
        return kept
    return Kept(kept.cutoff, (*kept.rows, *new), frozenset(keys), max(kept.newest, *map(_ts, new)))


def tail_bound(newest: str, margin: timedelta = TAIL_MARGIN) -> str | None:
    """Pure. The `ts` text a tail reads from: `margin` before `newest`, to the second in UTC, in the `YYYY-MM-DDTHH:MM:SS`
    form that sorts before every ISO `ts` of the same second. An empty window (`newest` "") tails from the start, "". None
    when `newest` is not an ISO time, which reads the whole window."""
    if not newest:
        return ""
    try:
        at = datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except ValueError:
        return None
    at = at.astimezone(UTC) if at.tzinfo is not None else at
    return (at - margin).strftime("%Y-%m-%dT%H:%M:%S")


def _count_and_tail(runs_dir: Path, cutoff: str, since: str) -> tuple[int, list[dict[str, object]]] | None:
    """Edge. The window's row count and its rows at or after `since`, from one statement; None on any failure."""
    try:
        opened = run_store.open_store(runs_dir)
    except (*run_store._DB_ERRORS, RuntimeError):
        return None
    if opened is None:
        return None
    conn, token = opened
    try:
        return count_and_tail_on(conn, token, cutoff, since)
    except run_store._DB_ERRORS:
        return None
    finally:
        conn.close()


def _read_window(runs_dir: Path, now: datetime, window_days: int) -> list[dict[str, object]]:
    """Edge. `chair_read_stale.read_chair_actions`, looked up at each call."""
    return chair_read_stale.read_chair_actions(runs_dir, now, window_days)


FullRead = Callable[[Path, datetime, int], Sequence[Mapping[str, object]]]
TailRead = Callable[[Path, str, str], "tuple[int, list[dict[str, object]]] | None"]


class ActionsCache:
    """The kept window for one chair process. `full_reads` counts the whole-window reads, `tail_reads` the ticks a tail
    alone answered."""

    def __init__(self, full: FullRead | None = None, tail: TailRead = _count_and_tail) -> None:
        self._full = full if full is not None else _read_window
        self._tail = tail
        self.kept: Kept | None = None
        self.full_reads = 0
        self.tail_reads = 0

    def read(self, runs_dir: Path, now: datetime, window_days: int) -> list[FrozenRow]:
        """Edge. The window's rows as `read_chair_actions(runs_dir, now, window_days)` returns them, each a `FrozenRow`."""
        cutoff = actions_cutoff(now, window_days)
        kept = self.kept
        since = tail_bound(kept.newest) if kept is not None and kept.cutoff == cutoff else None
        if kept is not None and since is not None:
            found = self._tail(runs_dir, cutoff, since)
            if found is not None:
                count, tail = found
                topped = topped_up(kept, tail)
                if len(topped.rows) == count:
                    self.tail_reads += 1
                    self.kept = topped
                    return list(topped.rows)
        self.kept = None
        rows = self._full(runs_dir, now, window_days)  # raises as the reader does: a table with no `target` column
        self.full_reads += 1
        self.kept = kept_from(cutoff, rows)  # an empty window too: the next tick tails it from the start
        return list(self.kept.rows)

    def tick_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """(full reads, tail reads) under the reader name `chair_actions`, in the shape of
        `chair_tick_cache.ReaderCache.tick_counts`: a tail read reuses the kept window."""
        return {"chair_actions": self.full_reads}, {"chair_actions": self.tail_reads}
