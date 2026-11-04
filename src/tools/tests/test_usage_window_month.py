"""gather_month and gather_daily_costs with injected readers: no store, no real run directory."""

from __future__ import annotations

from datetime import UTC, date, datetime

from agent_tools import usage_window


def _file(started: str, cost: float) -> tuple[datetime, dict]:
    return datetime.fromisoformat(started), {"summary": {"cost_usd": cost}}


def test_month_start_in_previous_utc_day_of_a_non_utc_zone(tmp_path):
    seen: list[tuple[str, str]] = []

    def store(since: str, until: str) -> float:
        seen.append((since, until))
        return 3.0

    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    month = usage_window.gather_month(
        tmp_path, now, "Asia/Tokyo", 100.0, cost_between=store, min_elapsed_fraction=0.1,
    )
    assert seen == [("2026-10-31T15:00:00+00:00", "2026-11-04T12:00:00+00:00")]
    assert month.start == datetime(2026, 10, 31, 15, 0, tzinfo=UTC)
    assert month.spent_usd == 3.0


def test_store_wins_over_files(tmp_path):
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    month = usage_window.gather_month(
        tmp_path, now, "UTC", 100.0, cost_between=lambda s, u: 12.5,
        usage=[_file("2026-11-02T00:00:00+00:00", 99.0)], min_elapsed_fraction=0.1,
    )
    assert month.spent_usd == 12.5


def test_store_none_falls_back_to_files(tmp_path):
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    usage = [
        _file("2026-11-01T00:00:00+09:00", 2.0),  # the Tokyo month start exactly
        _file("2026-10-31T23:59:59+09:00", 50.0),  # one second before it
        _file("2026-11-03T00:00:00+00:00", 1.5),
    ]
    month = usage_window.gather_month(
        tmp_path, now, "Asia/Tokyo", None, cost_between=lambda s, u: None, usage=usage, min_elapsed_fraction=0.1,
    )
    assert month.spent_usd == 3.5
    assert month.verdict == "unmeasured"


def test_daily_costs_across_a_month_boundary(tmp_path):
    calls: list[tuple[str, str]] = []

    def store(since: str, until: str) -> float | None:
        calls.append((since, until))
        return None if since.startswith("2026-11-01") else 1.0

    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    usage = [_file("2026-11-01T10:00:00+00:00", 4.0), _file("2026-11-01T23:59:59+00:00", 0.5)]
    costs = usage_window.gather_daily_costs(tmp_path, now, "UTC", 4, cost_between=store, usage=usage)
    assert costs == {
        date(2026, 10, 30): 1.0, date(2026, 10, 31): 1.0, date(2026, 11, 1): 4.5, date(2026, 11, 2): 1.0,
    }
    assert len(calls) == 4
    assert calls[0] == ("2026-10-30T00:00:00+00:00", "2026-10-31T00:00:00+00:00")


def test_daily_costs_use_zone_day_bounds(tmp_path):
    now = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)  # still Nov 1 in Los Angeles
    usage = [_file("2026-11-01T07:00:00+00:00", 2.0), _file("2026-11-01T06:59:59+00:00", 9.0)]
    costs = usage_window.gather_daily_costs(tmp_path, now, "America/Los_Angeles", 1, usage=usage)
    assert costs == {date(2026, 11, 1): 2.0}
