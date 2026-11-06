"""Pure burn rate and meter projection for the dash feed's live.burn block."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

METER_NAMES = ("five_hour", "weekly")


def _parse(ts: str) -> datetime | None:
    """Return the UTC datetime for an ISO string, or None when empty or unparseable."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _window_rate(turns: list[dict], now: datetime, minutes: int) -> float:
    """Return USD per minute over turns in (now - minutes, now]."""
    start = now - timedelta(minutes=minutes)
    stamped = ((_parse(str(t.get("ts", ""))), t.get("cost_usd") or 0) for t in turns)
    return sum(cost for at, cost in stamped if at is not None and start < at <= now) / minutes


def _minutes_left(resets_at: str, now: datetime) -> float:
    """Return minutes from now to resets_at, 0 when it is empty, unparseable or not after now."""
    at = _parse(resets_at)
    return 0.0 if at is None or at <= now else (at - now).total_seconds() / 60


def _meter(meter: dict, rate_5m: float, now: datetime) -> dict:
    """Project one meter to its reset, paced on the 5-minute rate."""
    used = meter["used_fraction"]
    spend = meter["window_spend_usd"]
    ceiling = meter["ceiling_fraction"]
    resets_at = meter.get("resets_at") or ""
    projected_spend = spend + rate_5m * _minutes_left(resets_at, now)
    projected = used * projected_spend / spend if spend else used
    return {
        "used_fraction": round(used, 4),
        "projected_fraction": round(projected, 4),
        "ceiling_fraction": round(ceiling, 4),
        "resets_at": resets_at,
        "over_ceiling": round(projected, 4) > ceiling > 0,
    }


def burn(turns: list[dict], meters: dict[str, dict], now: datetime) -> dict:
    """Return the live.burn block: 5m and 15m USD per minute and both meter projections."""
    rate_5m = _window_rate(turns, now, 5)
    return {
        "usd_per_min_5m": round(rate_5m, 4),
        "usd_per_min_15m": round(_window_rate(turns, now, 15), 4),
        "meters": {name: _meter(meters[name], rate_5m, now) for name in METER_NAMES},
    }
