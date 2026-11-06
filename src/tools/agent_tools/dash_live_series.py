"""Pure live series for the dash feed: throughput, calls, cache hit and per-hour counts.

A turn is a mapping with "ts" (ISO UTC), "host" and the four token counts. `now` is always passed in.
A bucket is kept only when its start lies in the window, so a turn in a minute after `now` is dropped.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

MINUTES = 60
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

Turn = Mapping[str, Any]


def _utc(moment: datetime) -> datetime:
    """A naive datetime counts as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _parse(ts: str) -> datetime:
    return _utc(datetime.fromisoformat(ts.replace("Z", "+00:00")))


def _floor(moment: datetime, step: timedelta) -> datetime:
    utc = _utc(moment)
    return utc.replace(minute=0, second=0, microsecond=0) if step == HOUR else utc.replace(second=0, microsecond=0)


def _starts(now: datetime, count: int, step: timedelta) -> list[datetime]:
    """Bucket starts, oldest first, the last one holding `now`."""
    last = _floor(now, step)
    return [last - step * (count - 1 - i) for i in range(count)]


def _group(records: Sequence[Turn], starts: Sequence[datetime], step: timedelta) -> dict[datetime, list[Turn]]:
    """Records per bucket start; records outside the window are dropped."""
    wanted = set(starts)
    pairs = [(_floor(_parse(r["ts"]), step), r) for r in records]
    grouped: dict[datetime, list[Turn]] = defaultdict(list)
    for start, record in pairs:
        if start in wanted:
            grouped[start].append(record)
    return grouped


def _tokens(turns: Sequence[Turn], key: str) -> int:
    return sum(t[key] for t in turns)


def _throughput_series(turns: Sequence[Turn], starts: Sequence[datetime]) -> list[dict[str, Any]]:
    grouped = _group(turns, starts, MINUTE)
    return [
        {
            "at": s.strftime(AT_FORMAT),
            "input": _tokens(grouped.get(s, []), "input_tokens"),
            "output": _tokens(grouped.get(s, []), "output_tokens"),
            "cache_read": _tokens(grouped.get(s, []), "cache_read_tokens"),
        }
        for s in starts
    ]


def throughput(turns: Sequence[Turn], now: datetime) -> dict[str, Any]:
    """System and per-host token series, 60 minutes each; hosts sorted, empty hosts omitted."""
    starts = _starts(now, MINUTES, MINUTE)
    in_window = [t for group in _group(turns, starts, MINUTE).values() for t in group]
    hosts = sorted({t["host"] for t in in_window})
    return {
        "system": _throughput_series(turns, starts),
        "hosts": [
            {"host": h, "series": _throughput_series([t for t in turns if t["host"] == h], starts)} for h in hosts
        ],
    }


def calls_per_min(turns: Sequence[Turn], now: datetime) -> list[dict[str, Any]]:
    """Turn count per minute over the last 60 minutes."""
    starts = _starts(now, MINUTES, MINUTE)
    grouped = _group(turns, starts, MINUTE)
    return [{"at": s.strftime(AT_FORMAT), "value": len(grouped.get(s, []))} for s in starts]


def _hit_pct(turns: Sequence[Turn]) -> float:
    """100 * cache_read / (input + cache_read + cache_write), 0.0 when the sum is zero."""
    read = _tokens(turns, "cache_read_tokens")
    total = _tokens(turns, "input_tokens") + read + _tokens(turns, "cache_write_tokens")
    return round(100 * read / total, 1) if total else 0.0


def cache_hit_pct(turns: Sequence[Turn], now: datetime) -> list[dict[str, Any]]:
    """Cache hit percent per minute over the last 60 minutes."""
    starts = _starts(now, MINUTES, MINUTE)
    grouped = _group(turns, starts, MINUTE)
    return [{"at": s.strftime(AT_FORMAT), "value": _hit_pct(grouped.get(s, []))} for s in starts]


def per_hour(events: Sequence[Mapping[str, Any]], now: datetime, hours: int) -> list[dict[str, Any]]:
    """Event count per hour over the last `hours` hours, oldest first."""
    starts = _starts(now, hours, HOUR)
    grouped = _group(events, starts, HOUR)
    return [{"at": s.strftime(AT_FORMAT), "value": len(grouped.get(s, []))} for s in starts]
