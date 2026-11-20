"""`cox usage`: the pacing policy, the chair meter and the usage assessment."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import math
from pathlib import Path

from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_lazy import pacing, route, run_store, usage_meter, usage_window

_USAGE_ASSESS_EXIT = {"go": 0, "go_degraded": 0, "hold": 3, "stop": 4}

_GATE_LEVELS = ("ticket", "phase", "epic", "full")


def _resolved_gate_level(runs_dir: Path) -> str:
    """`level` from `<runs_dir>/policy.gate.json`; `ticket` when absent, unreadable or unknown, never `full`."""
    text = _read_text_or_none(runs_dir / "policy.gate.json")
    try:
        raw = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        raw = None
    level = raw.get("level") if isinstance(raw, dict) else None
    return level if level in _GATE_LEVELS else "ticket"


def _is_number(value: object) -> bool:
    """A finite int or float; a bool, NaN or Infinity is not one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number_or(raw: object, default: float) -> float:
    return float(raw) if _is_number(raw) else default


def _weekly_pace_lanes(
    raw: object, default: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """`raw` as pairs if it is a list of two-number lists, so `[]` is `()`: no throttling. Else `default` whole."""
    ok = isinstance(raw, list) and all(
        isinstance(pair, list) and len(pair) == 2 and all(_is_number(n) for n in pair) for pair in raw
    )
    return tuple((float(a), float(b)) for a, b in raw) if ok else default


def _resolved_pacing_policy(runs_dir: Path) -> pacing.Policy:
    """The cartridge's resolved `policy.pacing`, read from
    `<runs_dir>/policy.pacing.json` when a landing step has dropped one there;
    the unmeasured-and-uncapped `usage_window.DEFAULT_POLICY` when the file is
    absent, unreadable, or not a mapping — same skip-not-raise contract as
    `records.ceiling_for`'s own `<run_id>.ceiling.json` handling. A key the
    file omits falls back to the matching `DEFAULT_POLICY` field, not to a
    guess. A malformed `weekly_target_fraction` or `weekly_pace_lanes` also
    falls back; the ladder falls back whole. The file's `max_in_flight` is not a `Policy` field: `chair run`
    reads it as its lane cap after the team cartridge's (`_chair_max_in_flight`, 3 when absent)."""
    default = usage_window.DEFAULT_POLICY
    text = _read_text_or_none(runs_dir / "policy.pacing.json")
    if text is None:
        return default
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return default
    if not isinstance(raw, dict):
        return default
    return pacing.Policy(
        pace_thresholds=tuple(raw.get("pace_thresholds", default.pace_thresholds)),
        tier_ladder=tuple(raw.get("tier_ladder", default.tier_ladder)),
        effort_ladder=tuple(raw.get("effort_ladder", default.effort_ladder)),
        min_headroom_usd=float(raw.get("min_headroom_usd", default.min_headroom_usd)),
        hard_stop_fraction=float(raw.get("hard_stop_fraction", default.hard_stop_fraction)),
        weekly_hard_stop_fraction=float(raw.get("weekly_hard_stop_fraction", default.weekly_hard_stop_fraction)),
        weekly_target_fraction=_number_or(raw.get("weekly_target_fraction"), default.weekly_target_fraction),
        weekly_pace_lanes=_weekly_pace_lanes(raw.get("weekly_pace_lanes"), default.weekly_pace_lanes),
    )


def _chair_meter(
    local: usage_meter.Meter | None, now: datetime.datetime, runs_dir,
) -> tuple[usage_meter.Meter | None, str]:
    """A fresh local reading is 'meter' and beats any store row, including a newer-looking row from an older plan;
    else a store row under 15 minutes old is 'meter, store'; else the local reading (stale, or None) with 'est',
    so a stale local reading is still carried forward by `usable_percentage`."""
    if local is not None and usage_meter.fresh(local, now):
        return local, "meter"
    doc = run_store.latest_chair_meter(Path(runs_dir))
    chair = usage_meter.parse(doc) if doc else None
    # A naive `observed_at` cannot be compared with an aware `now`; `fresh` would raise, so it is not adopted.
    if (
        chair is not None and chair.observed_at.tzinfo is not None
        and usage_meter.fresh(chair, now, max_age=datetime.timedelta(minutes=15))
    ):
        return chair, "meter, store"
    return local, "est"


def _reconcile_ceilings(meter: usage_meter.Meter | None, source: str, runs_dir) -> None:
    """Edge. Run before any learned ceiling is read: a plan change drops the old plan's ceilings here. Only a fresh
    local reading (source 'meter') may reconcile, the same reading that may calibrate."""
    if meter is not None and source == "meter":
        usage_meter.reconcile_implied_ceilings(meter, Path(runs_dir))


def _tolerant_cost_since(runs_dir, since: str, until: str | None = None) -> float | None:
    """Edge. The store's spend since `since` (and before `until`, when given), or None on any store error, so a broken
    store falls back to the usage files instead of failing the gate and every surface that narrates it (route context
    still prints its docket)."""
    try:
        bounds = (since,) if until is None else (since, until)
        return run_store.cost_since(Path(runs_dir), *bounds)
    except (*run_store.db_error_types(), ValueError, TypeError, RuntimeError):  # RuntimeError: psycopg not installed
        return None


def _budget_zone(budget_timezone: str | None, weekly_reset: str | None) -> str:
    """Pure. The month's zone: the budget's own, else the weekly reset's, else UTC."""
    reset = usage_window.parse_weekly_reset(weekly_reset)
    return budget_timezone or (reset.tz if reset is not None else "UTC")


def _month_for(
    runs_dir, now: datetime.datetime, monthly_budget_usd: float | None, budget_timezone: str | None,
    weekly_reset: str | None, min_elapsed_fraction: float,
):
    """Edge. The month's spend against its budget, or None with no budget set. The store's spend wins."""
    if monthly_budget_usd is None:
        return None
    return usage_window.gather_month(
        runs_dir, now, _budget_zone(budget_timezone, weekly_reset), monthly_budget_usd,
        cost_between=lambda since, until: _tolerant_cost_since(runs_dir, since, until),
        min_elapsed_fraction=min_elapsed_fraction,
    )


def _profile_month(runs_dir, profile: dict, now: datetime.datetime, min_elapsed_fraction: float):
    """Edge. `_month_for` read from a parsed profile's three keys, for every caller that holds a profile."""
    return _month_for(
        runs_dir, now, profile.get("monthly_budget_usd"), profile.get("budget_timezone"),
        profile.get("weekly_reset"), min_elapsed_fraction,
    )


def _usage_assessment(
    runs_dir, window_ceiling_usd: float | None = None, weekly_ceiling_usd: float | None = None,
    weekly_reset: str | None = None, monthly_budget_usd: float | None = None, budget_timezone: str | None = None,
) -> pacing.Assessment:
    """Computed once via the gatherer and shared by every surface that
    narrates it: `usage assess`, `route context`'s docket line, and `route
    launch`'s gate all call this so the same window yields the same reason.
    """
    now = datetime.datetime.now(datetime.UTC)
    reset = usage_window.parse_weekly_reset(weekly_reset)
    meter, source = _chair_meter(usage_meter.read(), now, runs_dir)
    _reconcile_ceilings(meter, source, runs_dir)
    policy = _resolved_pacing_policy(Path(runs_dir))
    month = _month_for(runs_dir, now, monthly_budget_usd, budget_timezone, weekly_reset, policy.min_elapsed_fraction)
    if (
        monthly_budget_usd is not None and meter is None
        and window_ceiling_usd is None and weekly_ceiling_usd is None
        and usage_meter.implied_ceiling("five_hour", now) is None and usage_meter.implied_ceiling("weekly", now) is None
    ):
        # The budget is the only limit: the five-hour and weekly windows are neither gathered, shown nor judged.
        return pacing.assess(usage_meter.unlimited_window(now), policy, now, month=month)
    usage = usage_window.read_usage(runs_dir, now, reset)
    # Calibration divides spend up to `now` by the reading's percentage, so only a fresh local reading may write it.
    calibrate = source == "meter"

    if meter is not None and meter.five_hour is None:
        # This plan has no five-hour limit: no estimate fallback, no ceiling read, nothing to calibrate.
        window = usage_meter.unlimited_window(now)
    else:
        five_hour_ceiling = usage_meter.implied_ceiling("five_hour", now)
        if five_hour_ceiling is None:
            five_hour_ceiling = window_ceiling_usd
        estimate_window = usage_window.gather(runs_dir, now, ceiling_usd=five_hour_ceiling, usage=usage)
        five_hour_usable = (
            usage_meter.usable_percentage(
                "five_hour", meter.five_hour, meter.observed_at, now, Path(runs_dir), five_hour_ceiling,
            )
            if meter is not None else None
        )
        meter_window = (
            usage_meter.as_window(
                dataclasses.replace(meter.five_hour, used_percentage=five_hour_usable), now, datetime.timedelta(hours=5),
            )
            if five_hour_usable is not None else None
        )
        window = usage_meter.prefer(meter_window, estimate_window)
        if calibrate:
            usage_meter.record_implied_ceiling("five_hour", meter.five_hour, Path(runs_dir), now)

    weekly_ceiling = usage_meter.implied_ceiling("weekly", now)
    if weekly_ceiling is None:
        weekly_ceiling = weekly_ceiling_usd
    # The store's spend, the same source the chair's facts and the console read: a lane host's own usage files
    # also hold runs fetched from or synced with other machines (one lane host read $4093 against a store $1349).
    estimate_weekly = usage_window.gather_weekly(
        runs_dir, now, weekly_ceiling, usage=usage, reset=reset,
        store_spend=lambda since: _tolerant_cost_since(runs_dir, since),
    )
    weekly_usable = (
        usage_meter.usable_percentage(
            "weekly", meter.seven_day, meter.observed_at, now, Path(runs_dir), weekly_ceiling,
        )
        if meter is not None else None
    )
    meter_weekly = (
        usage_meter.as_window(
            dataclasses.replace(meter.seven_day, used_percentage=weekly_usable), now, datetime.timedelta(days=7),
        )
        if weekly_usable is not None else None
    )
    weekly = usage_meter.prefer(meter_weekly, estimate_weekly)
    if calibrate:
        usage_meter.record_implied_ceiling("weekly", meter.seven_day, Path(runs_dir), now)

    return pacing.assess(window, policy, now, weekly=weekly, **({} if month is None else {"month": month}))


def _usage_assess(a: argparse.Namespace) -> int:
    # Resolved the same tolerant way `_route_context` resolves its profile:
    # a missing or unreadable profile just means no ceiling, not a raise.
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        profile = {}
    result = _usage_assessment(
        a.runs_dir, profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd"), profile.get("weekly_reset"),
        profile.get("monthly_budget_usd"), profile.get("budget_timezone"),
    )
    if a.json:
        d = dataclasses.asdict(result)
        d["hold_until"] = result.hold_until.isoformat() if result.hold_until else None
        print(json.dumps(d, indent=2))
    else:
        month_line = (
            f" [month to date ${result.month_spent_usd:.2f}, projected ${result.month_projected_usd:.2f}, "
            f"{result.month_verdict}]"
            if result.month_verdict is not None else ""
        )
        print(f"{result.verdict}: {result.reason}{month_line}")
    return _USAGE_ASSESS_EXIT[result.verdict]
