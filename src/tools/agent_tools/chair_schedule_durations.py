"""Estimate how long a phase takes from finished-phase records. Pure: no clock, no file."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

SIZE_KEY = "size"
MIX_KEY = "mix"
START_KEY = "build_start"
END_KEY = "approve_time"


@dataclass(frozen=True)
class Estimate:
    seconds: float
    tier: str  # "size_mix", "size", "all" or "default"
    n: int  # usable records behind the estimate; 0 for "default"


def span_seconds(record: Mapping[str, str]) -> float | None:
    """Build-to-approve seconds, or None when a field is missing, unparseable or the span is negative."""
    try:
        span = (datetime.fromisoformat(record[END_KEY]) - datetime.fromisoformat(record[START_KEY])).total_seconds()
    except KeyError, TypeError, ValueError:
        return None
    return span if span >= 0 else None


def median(values: Sequence[float]) -> float:
    """Middle value; the mean of the middle pair for an even count. Needs a non-empty sequence."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def estimate_duration(records: Sequence[Mapping[str, str]], size: str, mix: str, default_seconds: float) -> Estimate:
    spans = [(r.get(SIZE_KEY), r.get(MIX_KEY), s) for r in records if (s := span_seconds(r)) is not None]
    tiers = (
        ("size_mix", [s for z, m, s in spans if z == size and m == mix]),
        ("size", [s for z, _, s in spans if z == size]),
        ("all", [s for _, _, s in spans]),
    )
    hit = next(((tier, found) for tier, found in tiers if found), None)
    if hit is None:
        return Estimate(default_seconds, "default", 0)
    return Estimate(median(hit[1]), hit[0], len(hit[1]))
