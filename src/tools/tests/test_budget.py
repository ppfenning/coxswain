from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent_tools.budget import month_bounds, month_budget, rolling_daily_avg


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _ymd(moment: datetime) -> tuple[int, int, int]:
    return moment.year, moment.month, moment.day


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        (_utc(2026, 10, 15, 12), (2026, 10, 1), (2026, 11, 1)),  # 31 days
        (_utc(2026, 11, 15, 12), (2026, 11, 1), (2026, 12, 1)),  # 30 days
        (_utc(2028, 2, 10, 12), (2028, 2, 1), (2028, 3, 1)),  # leap February
        (_utc(2027, 2, 10, 12), (2027, 2, 1), (2027, 3, 1)),  # non-leap February
        (_utc(2026, 12, 15, 12), (2026, 12, 1), (2027, 1, 1)),  # December rolls the year
        (_utc(2026, 1, 31, 23, 59, 59), (2026, 1, 1), (2026, 2, 1)),  # last second
        (_utc(2026, 2, 1, 0, 0, 0), (2026, 2, 1), (2026, 3, 1)),  # first second after
    ],
)
def test_month_bounds_calendar(now, start, end):
    got_start, got_end = month_bounds(now, "UTC")
    assert (_ymd(got_start), _ymd(got_end)) == (start, end)
    assert (got_start.hour, got_end.hour) == (0, 0)


def test_month_bounds_local_month_differs_from_utc():
    # 03:00 UTC on 1 Dec is 19:00 on 30 Nov in Los Angeles.
    start, end = month_bounds(_utc(2026, 12, 1, 3), "America/Los_Angeles")
    assert (_ymd(start), _ymd(end)) == ((2026, 11, 1), (2026, 12, 1))
    assert start.utcoffset() == timedelta(hours=-7)


def test_month_bounds_keep_local_midnight_across_dst():
    start, end = month_bounds(_utc(2026, 3, 15, 12), "America/New_York")
    assert (start.hour, end.hour) == (0, 0)
    assert (start.utcoffset(), end.utcoffset()) == (timedelta(hours=-5), timedelta(hours=-4))


def test_projection_uses_fractional_days_remaining():
    # 15.5 of 30 days elapsed: average 2.0 a day, 14.5 days left.
    got = month_budget(31.0, 50.0, _utc(2026, 11, 16, 12), "UTC", 0.2)
    assert got.daily_avg_usd == pytest.approx(2.0)
    assert got.projected_usd == pytest.approx(60.0)
    assert got.elapsed_fraction == pytest.approx(15.5 / 30)
    assert got.fraction == pytest.approx(0.62)


def test_daily_average_floors_elapsed_days_at_one():
    # Six hours in: elapsed is a quarter day, so the divisor stays 1.0.
    got = month_budget(3.0, None, _utc(2026, 11, 1, 6), "UTC", 0.2)
    assert got.daily_avg_usd == pytest.approx(3.0)


def test_verdicts():
    mid = _utc(2026, 11, 16, 12)
    assert month_budget(31.0, None, mid, "UTC", 0.2).verdict == "unmeasured"
    assert month_budget(31.0, None, mid, "UTC", 0.2).fraction is None
    assert month_budget(50.0, 50.0, mid, "UTC", 0.2).verdict == "over_budget"
    assert month_budget(31.0, 50.0, mid, "UTC", 0.2).verdict == "over_pace"
    assert month_budget(31.0, 50.0, mid, "UTC", 0.9).verdict == "on_track"
    assert month_budget(10.0, 50.0, mid, "UTC", 0.2).verdict == "on_track"


def test_month_budget_is_frozen():
    got = month_budget(1.0, 5.0, _utc(2026, 11, 16), "UTC", 0.2)
    with pytest.raises(FrozenInstanceError):
        got.spent_usd = 2.0  # type: ignore[misc]


def test_days_left_is_zero_on_the_last_local_day():
    got = month_budget(1.0, 5.0, _utc(2026, 11, 30, 23, 59, 59), "UTC", 0.2)
    assert got.days_left == 0
    assert month_budget(1.0, 5.0, _utc(2026, 11, 1), "UTC", 0.2).days_left == 29


def test_days_left_uses_the_local_date():
    # 03:00 UTC on 1 Dec is still 30 Nov in Los Angeles.
    now = datetime(2026, 12, 1, 3, tzinfo=ZoneInfo("UTC"))
    assert month_budget(1.0, 5.0, now, "America/Los_Angeles", 0.2).days_left == 0


def test_rolling_daily_avg_counts_missing_days_as_zero():
    today = date(2026, 11, 4)
    costs = {
        today: 3.0,
        today - timedelta(days=2): 6.0,
        today - timedelta(days=3): 100.0,  # outside the 3-day window
    }
    assert rolling_daily_avg(costs, today, 3) == pytest.approx(3.0)
