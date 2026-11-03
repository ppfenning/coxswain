"""Reads the claude-code status-line rate-limit JSON into a `Meter` and turns an entry into a `pacing.Window`.
A `Meter.five_hour` of None means this plan has no five-hour limit, not a missing reading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import pacing, run_store

__all__ = [
    "DEFAULT_PATH",
    "IMPLIED_PATH",
    "STATE_DIR",
    "LastMeter",
    "Meter",
    "MeterEntry",
    "as_window",
    "fresh",
    "implied_ceiling",
    "kinds_to_drop",
    "parse",
    "prefer",
    "read",
    "reconcile_implied_ceilings",
    "record_implied_ceiling",
    "unlimited_window",
    "usable_percentage",
]

_SPANS = {"five_hour": timedelta(hours=5), "weekly": timedelta(days=7)}
_MIN_ELAPSED = {"five_hour": timedelta(hours=1), "weekly": timedelta(days=1)}
_FRESH_AGE = timedelta(minutes=15)
_DROP_TOLERANCE_PCT = 2.0  # a weekly fall this small is rounding noise, not a plan change

STATE_DIR = Path.home() / ".local" / "state" / "coxswain"
DEFAULT_PATH = STATE_DIR / "rate-limits.json"
IMPLIED_PATH = STATE_DIR / "implied-ceilings.json"

_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class MeterEntry:
    used_percentage: float
    resets_at: datetime


@dataclass(frozen=True)
class Meter:
    """`five_hour` is None when this plan has no five-hour limit. It is not a missing reading."""

    five_hour: MeterEntry | None
    seven_day: MeterEntry
    observed_at: datetime


def _entry(doc: dict) -> MeterEntry:
    return MeterEntry(
        used_percentage=float(doc["used_percentage"]),
        resets_at=datetime.fromtimestamp(doc["resets_at"], tz=UTC),
    )


def parse(doc: dict) -> Meter | None:
    """A null or absent `five_hour` parses to `five_hour=None`: the plan has no five-hour limit.
    A malformed non-null `five_hour`, a bad `seven_day` or a bad `observed_at` returns None."""
    five_hour_doc = doc.get("five_hour")
    try:
        return Meter(
            five_hour=None if five_hour_doc is None else _entry(five_hour_doc),
            seven_day=_entry(doc["seven_day"]),
            observed_at=datetime.fromisoformat(doc["observed_at"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def fresh(meter: Meter, now: datetime, max_age: timedelta = timedelta(minutes=15)) -> bool:
    return now - meter.observed_at <= max_age


def _burn(used: float, elapsed_hours: float) -> float:
    """No elapsed time, e.g. clock skew past the window start, never divides: spend then is infinite pace, as in `pacing._ratio`."""
    if elapsed_hours > 0:
        return used / elapsed_hours
    if used > 0:
        return float("inf")
    return 0.0


def as_window(entry: MeterEntry, now: datetime, span: timedelta) -> pacing.Window:
    """`end` is the reset, not `now`: an `end` of `now` reads as a spent block, a zero countdown and full elapsed pace."""
    start = entry.resets_at - span
    return pacing.Window(
        start=start,
        end=entry.resets_at,
        spent_usd=entry.used_percentage,
        ceiling_usd=100.0,
        burn_usd_per_hour=_burn(entry.used_percentage, (now - start) / _HOUR),
        runs_in_flight=0,
    )


def unlimited_window(now: datetime) -> pacing.Window:
    """A plan with no five-hour limit: no ceiling and nothing spent, so `pacing.assess` reads it as unmeasured and says go."""
    return pacing.Window(
        start=now,
        end=now + _SPANS["five_hour"],
        spent_usd=0.0,
        ceiling_usd=None,
        burn_usd_per_hour=0.0,
        runs_in_flight=0,
    )


def read(path: Path = DEFAULT_PATH) -> Meter | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return None
    return parse(doc) if isinstance(doc, dict) else None


def prefer(meter_window: pacing.Window | None, estimate_window: pacing.Window) -> pacing.Window:
    return meter_window if meter_window is not None else estimate_window


def _load_json_object(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return {}
    return doc if isinstance(doc, dict) else {}


def record_implied_ceiling(
    kind: str,
    meter_entry: MeterEntry,
    runs_dir: Path,
    now: datetime,
    path: Path = IMPLIED_PATH,
) -> None:
    """Calibrates over the meter entry's own window, not `usage_window.gather`'s floating five-hour block.
    Writes nothing below the 5% floor, nor before an hour (five_hour) or a day (weekly) of that window has elapsed."""
    if meter_entry.used_percentage < 5:
        return
    span = _SPANS[kind]
    window_start = meter_entry.resets_at - span
    if now - window_start < _MIN_ELAPSED[kind]:
        return
    dollars = run_store.cost_since(runs_dir, window_start.isoformat())
    if dollars is None:
        return
    implied_usd = dollars / (meter_entry.used_percentage / 100)
    doc = _load_json_object(path)
    doc[kind] = {"ceiling_usd": implied_usd, "observed_at": now.isoformat()}
    path.write_text(json.dumps(doc))


def implied_ceiling(
    kind: str,
    now: datetime,
    path: Path = IMPLIED_PATH,
    max_age: timedelta = timedelta(days=7),
) -> float | None:
    doc = _load_json_object(path)
    try:
        entry = doc[kind]
        ceiling_usd = float(entry["ceiling_usd"])
        observed_at = datetime.fromisoformat(entry["observed_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if now - observed_at <= max_age:
        return ceiling_usd
    return None


@dataclass(frozen=True)
class LastMeter:
    """The previous reading, kept under `last_meter` in the implied-ceilings file."""

    has_five_hour: bool
    weekly_used: float
    weekly_resets_at: datetime
    observed_at: datetime


def _last_meter_from_doc(doc: dict) -> LastMeter | None:
    try:
        record = doc["last_meter"]
        return LastMeter(
            has_five_hour=bool(record["has_five_hour"]),
            weekly_used=float(record["weekly_used"]),
            weekly_resets_at=datetime.fromisoformat(record["weekly_resets_at"]),
            observed_at=datetime.fromisoformat(record["observed_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _last_meter_to_doc(last: LastMeter) -> dict:
    return {
        "has_five_hour": last.has_five_hour,
        "weekly_used": last.weekly_used,
        "weekly_resets_at": last.weekly_resets_at.isoformat(),
        "observed_at": last.observed_at.isoformat(),
    }


def _last_meter_of(meter: Meter) -> LastMeter:
    return LastMeter(
        has_five_hour=meter.five_hour is not None,
        weekly_used=meter.seven_day.used_percentage,
        weekly_resets_at=meter.seven_day.resets_at,
        observed_at=meter.observed_at,
    )


def kinds_to_drop(
    previous: LastMeter | None,
    meter: Meter,
    spend_usd: float | None,
    weekly_ceiling_usd: float | None,
) -> frozenset[str]:
    """Which learned ceilings a new reading invalidates. A five-hour window that appeared or vanished drops
    `five_hour`. A weekly fall larger than the spend since `previous` explains, plus tolerance, drops both,
    unless `previous.weekly_resets_at` had already passed at `meter.observed_at`: that is an ordinary reset."""
    if previous is None:
        return frozenset()
    shape = frozenset({"five_hour"}) if previous.has_five_hour != (meter.five_hour is not None) else frozenset()
    fall = previous.weekly_used - meter.seven_day.used_percentage
    ordinary_reset = previous.weekly_resets_at <= meter.observed_at
    explained = spend_usd / weekly_ceiling_usd * 100 if spend_usd is not None and weekly_ceiling_usd else 0.0
    plan_change = not ordinary_reset and fall > explained + _DROP_TOLERANCE_PCT
    return shape | frozenset({"five_hour", "weekly"}) if plan_change else shape


def reconcile_implied_ceilings(
    meter: Meter,
    runs_dir: Path,
    path: Path = IMPLIED_PATH,
) -> frozenset[str]:
    """Run before any ceiling is used. Drops what `kinds_to_drop` names and always records this reading
    as `last_meter`. A missing or unreadable file counts as no prior reading. Returns the dropped kinds."""
    doc = _load_json_object(path)
    previous = _last_meter_from_doc(doc)
    spend = None if previous is None else run_store.cost_since(runs_dir, previous.observed_at.isoformat())
    dropped = kinds_to_drop(previous, meter, spend, implied_ceiling("weekly", meter.observed_at, path))
    kept = {key: value for key, value in doc.items() if key not in dropped}
    path.write_text(json.dumps({**kept, "last_meter": _last_meter_to_doc(_last_meter_of(meter))}))
    return dropped


def usable_percentage(
    kind: str,
    entry: MeterEntry,
    observed_at: datetime,
    now: datetime,
    runs_dir: Path,
    ceiling_usd: float | None,
    path: Path = IMPLIED_PATH,
) -> float | None:
    """A second path alongside `fresh`: a reading within 15 minutes reads at face value, one past its
    `resets_at` is never usable, and one in between is carried forward by the dollars spent since
    `observed_at` over the implied (or caller-supplied) ceiling, same as `fresh` used to drop outright."""
    if now - observed_at <= _FRESH_AGE:
        return entry.used_percentage
    if now >= entry.resets_at:
        return None
    ceiling = implied_ceiling(kind, now, path) or ceiling_usd
    spent_since = run_store.cost_since(runs_dir, observed_at.isoformat())
    if spent_since is None or not ceiling:
        return None
    return entry.used_percentage + (spent_since / ceiling) * 100
