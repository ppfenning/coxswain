"""Pure assembly of the `live.agents` list of the dash feed: one row per agent with a recent turn."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from itertools import groupby
from types import MappingProxyType

RATE_WINDOW_S = 300
# The catalog's `context_window` is used when set; else this constant, which holds only the figure the catalog itself states (null there means unknown).
_FALLBACK_WINDOWS: Mapping[str, int] = MappingProxyType(
    dict.fromkeys(("claude-haiku-4-5-20251001", "claude-haiku-4-5", "haiku"), 200_000)
)


def default_context_windows(catalog: Mapping | None) -> dict[str, int]:
    """model -> window; a catalog entry's own `context_window` (id and aliases) wins over the constant fallback."""
    models = catalog.get("models") if isinstance(catalog, Mapping) else None
    entries = [e for e in models if isinstance(e, Mapping)] if isinstance(models, list) else []
    from_catalog = {
        str(name): entry["context_window"]
        for entry in entries
        if isinstance(entry.get("context_window"), int) and entry["context_window"] > 0 and entry.get("id") is not None
        for name in (entry["id"], *(entry["aliases"] if isinstance(entry.get("aliases"), list) else []))
    }
    return {**_FALLBACK_WINDOWS, **from_catalog}


def agents(
    turns: Sequence[Mapping],
    now: datetime | str,
    context_windows: Mapping[str, int],
    live_within_s: int = 600,
) -> list[dict]:
    """Rows for every (source, agent_id) whose newest turn is within `live_within_s` of `now`, busiest first then by id."""
    at = _ts(now)
    stamped = sorted(
        ((_ts(t["ts"]), t) for t in turns), key=lambda p: (str(p[1].get("source")), str(p[1].get("agent_id")), p[0])
    )
    groups = [list(g) for _, g in groupby(stamped, key=lambda p: (str(p[1].get("source")), str(p[1].get("agent_id"))))]
    live = [g for g in groups if _age_s(at, g[-1][0]) <= live_within_s]
    rows = [_row(g, at, context_windows) for g in live]
    return sorted(rows, key=lambda r: (-r["tokens_per_min"], r["id"]))


def _row(group: Sequence[tuple[datetime, Mapping]], now: datetime, windows: Mapping[str, int]) -> dict:
    oldest, newest = group[0][0], group[-1]
    last = newest[1]
    recent = [t for ts, t in group if _age_s(now, ts) <= RATE_WINDOW_S]
    rate = sum(_n(t, k) for t in recent for k in ("input_tokens", "output_tokens", "cache_read_tokens")) / (
        RATE_WINDOW_S / 60
    )
    return {
        "id": last["agent_id"],
        "kind": last["kind"],
        "model": last["model"],
        "host": last["host"],
        "serves": last.get("task") or last.get("initiative") or "",
        "tokens_per_min": round(rate),
        "cost_usd": round(sum(_n(t, "cost_usd") for _, t in group), 4),
        "elapsed_s": int(_age_s(now, oldest)),
        "context_fill": _context_fill(last, windows),
    }


def _context_fill(turn: Mapping, windows: Mapping[str, int]) -> float:
    """Newest turn's input + cache read + cache write tokens over the model's window; 0.0 for an unknown model, capped at 1.0."""
    window = windows.get(turn["model"])
    if not isinstance(window, int) or window <= 0:
        return 0.0
    used = sum(_n(turn, k) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
    return round(min(1.0, used / window), 3)


def _n(turn: Mapping, key: str) -> float:
    return float(turn.get(key) or 0)


def _age_s(now: datetime, ts: datetime) -> float:
    """Seconds from `ts` to `now`; a turn stamped after `now` (host clock skew) is age 0."""
    return max(0.0, (now - ts).total_seconds())


def _ts(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
