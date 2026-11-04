"""Pure calendar-month budget math: bounds, pace projection and a verdict.

No clock and no I/O here. The caller passes `now`, the IANA zone name and
the spend so far.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

_DAY_SECONDS = 86400.0


@dataclass(frozen=True)
class MonthBudget:
    """One calendar month's spend against an optional budget."""

    start: datetime
    end: datetime
    spent_usd: float
    budget_usd: float | None
    elapsed_fraction: float
    daily_avg_usd: float
    projected_usd: float
    fraction: float | None
    days_left: int
    verdict: str


def month_bounds(now: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """Aware start and end of the calendar month holding `now` in `tz_name`; end is the next month's start."""
    local = now.astimezone(ZoneInfo(tz_name))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0, fold=0)
    next_year, next_month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start, start.replace(year=next_year, month=next_month)


def _seconds_between(earlier: datetime, later: datetime) -> float:
    """Real elapsed seconds; same-zone aware subtraction would ignore a DST change."""
    return (later.astimezone(UTC) - earlier.astimezone(UTC)).total_seconds()


def _verdict(
    spent_usd: float,
    budget_usd: float | None,
    projected_usd: float,
    elapsed_fraction: float,
    min_elapsed_fraction: float,
) -> str:
    if budget_usd is None:
        return "unmeasured"
    if spent_usd >= budget_usd:
        return "over_budget"
    if projected_usd > budget_usd and elapsed_fraction >= min_elapsed_fraction:
        return "over_pace"
    return "on_track"


def month_budget(
    spent_usd: float,
    budget_usd: float | None,
    now: datetime,
    tz_name: str,
    min_elapsed_fraction: float,
) -> MonthBudget:
    """Fill a MonthBudget; daily average divides by elapsed days floored at 1.0."""
    start, end = month_bounds(now, tz_name)
    elapsed_s = _seconds_between(start, now)
    remaining_s = _seconds_between(now, end)
    elapsed_fraction = elapsed_s / (elapsed_s + remaining_s)
    daily_avg_usd = spent_usd / max(elapsed_s / _DAY_SECONDS, 1.0)
    projected_usd = spent_usd + daily_avg_usd * (remaining_s / _DAY_SECONDS)
    today = now.astimezone(ZoneInfo(tz_name)).date()
    return MonthBudget(
        start=start,
        end=end,
        spent_usd=spent_usd,
        budget_usd=budget_usd,
        elapsed_fraction=elapsed_fraction,
        daily_avg_usd=daily_avg_usd,
        projected_usd=projected_usd,
        fraction=spent_usd / budget_usd if budget_usd else None,
        days_left=(end.date() - today).days - 1,
        verdict=_verdict(spent_usd, budget_usd, projected_usd, elapsed_fraction, min_elapsed_fraction),
    )


def rolling_daily_avg(daily_costs: Mapping[date, float], today: date, days: int) -> float:
    """Sum of the `days` local dates ending with `today` over `days`; a missing date counts as 0."""
    window = (today - timedelta(days=offset) for offset in range(days))
    return sum(daily_costs.get(day, 0.0) for day in window) / days
