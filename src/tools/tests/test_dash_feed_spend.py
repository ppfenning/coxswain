"""`dash_feed._spend` picks its source in order: fresh local meter, fresh store meter, then the estimate."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_run_store import ROW, store

from agent_tools import cli, dash_feed, route, usage_meter, usage_window

NOW = "2026-10-09T12:00:00Z"
NOW_DT = datetime(2026, 10, 9, 12, tzinfo=UTC)
SUNDAY_4AM_UTC = usage_window.WeeklyReset(weekday=6, hour=4, minute=0, tz="UTC")


def _meter(five_pct: float, seven_pct: float, observed_at: datetime, weekly_resets_at: datetime | None = None):
    return usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(used_percentage=five_pct, resets_at=NOW_DT + timedelta(hours=2)),
        seven_day=usage_meter.MeterEntry(
            used_percentage=seven_pct, resets_at=weekly_resets_at or datetime(2026, 10, 11, 4, tzinfo=UTC),
        ),
        observed_at=observed_at,
    )


def _doc(meter: usage_meter.Meter) -> dict:
    """The store row's JSON shape: `usage_meter.parse` reads epoch seconds for `resets_at`."""
    return {
        "five_hour": {"used_percentage": meter.five_hour.used_percentage, "resets_at": meter.five_hour.resets_at.timestamp()},
        "seven_day": {"used_percentage": meter.seven_day.used_percentage, "resets_at": meter.seven_day.resets_at.timestamp()},
        "observed_at": meter.observed_at.isoformat(),
    }


def _no_reset() -> None:
    return None


def _sunday() -> usage_window.WeeklyReset:
    return SUNDAY_4AM_UTC


def _usage_file(runs_dir: Path, name: str, started: datetime, cost: float) -> None:
    path = runs_dir / f"{name}.usage.json"
    path.write_text(json.dumps({"summary": {"cost_usd": cost, "started_at": started.isoformat()}}))
    os.utime(path, (started.timestamp(), started.timestamp()))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No pacing file, profile, implied-ceiling file or ccusage launch is read."""
    monkeypatch.setattr(dash_feed, "_hard_stop_fraction", lambda runs_dir: 0.9)
    monkeypatch.setattr(dash_feed, "_profile_ceilings", lambda: (100.0, 100.0))
    monkeypatch.setattr(usage_meter, "implied_ceiling", lambda kind, now, *a, **k: None)
    real_gather = usage_window.gather

    def offline_gather(runs_dir, now, *, ceiling_usd=None, start=None):
        def no_ccusage(*args, **kwargs):
            raise OSError("no ccusage in tests")

        return real_gather(runs_dir, now, run=no_ccusage, ceiling_usd=ceiling_usd, start=start)

    monkeypatch.setattr(dash_feed.usage_window, "gather", offline_gather)


def test_stale_local_and_fresh_store_yield_the_stores_fractions(tmp_path):
    stale = _meter(1, 2, NOW_DT - timedelta(hours=3))
    stored = _meter(30, 60, NOW_DT - timedelta(minutes=5))

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: stale, read_store=lambda runs_dir: _doc(stored), read_reset=_no_reset,
    )

    assert spend["five_hour_fraction"] == 0.30
    assert spend["weekly_fraction"] == 0.60
    assert (spend["five_hour_source"], spend["weekly_source"]) == ("meter, store", "meter, store")
    assert spend["spend_source"] == "meter store"
    assert spend["spend_age_s"] == 300
    assert spend["weekly_resets_at"] == stored.seven_day.resets_at.isoformat()


def test_missing_local_meter_also_falls_to_the_store(tmp_path):
    stored = _meter(30, 60, NOW_DT - timedelta(minutes=5))

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: None, read_store=lambda runs_dir: _doc(stored), read_reset=_no_reset,
    )

    assert spend["spend_source"] == "meter store"


def test_fresh_local_meter_wins_over_the_store(tmp_path):
    local = _meter(8, 58, NOW_DT - timedelta(minutes=2))
    stored = _meter(30, 60, NOW_DT - timedelta(minutes=1))

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: local, read_store=lambda runs_dir: _doc(stored), read_reset=_no_reset,
    )

    assert (spend["five_hour_fraction"], spend["weekly_fraction"]) == (0.08, 0.58)
    assert (spend["five_hour_source"], spend["weekly_source"]) == ("meter", "meter")
    assert spend["spend_source"] == "local meter"
    assert spend["spend_age_s"] == 120


def test_store_meter_with_a_naive_observed_at_is_not_adopted(tmp_path):
    naive = {**_doc(_meter(30, 60, NOW_DT)), "observed_at": "2026-10-09T11:59:00"}

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: None, read_store=lambda runs_dir: naive, read_reset=_no_reset,
    )

    assert spend["spend_source"] == "estimate"


def test_a_failing_store_reader_falls_through_to_the_estimate(tmp_path):
    def broken(runs_dir):
        raise RuntimeError("psycopg not installed")

    spend = dash_feed._spend(tmp_path, NOW, read_local=lambda: None, read_store=broken, read_reset=_no_reset)

    assert spend["spend_source"] == "estimate"
    assert spend["spend_age_s"] is None


def test_estimate_without_any_meter_excludes_spend_from_the_previous_week(tmp_path):
    _usage_file(tmp_path, "last-week", datetime(2026, 10, 3, 10, tzinfo=UTC), cost=40.0)
    _usage_file(tmp_path, "this-week", datetime(2026, 10, 6, 10, tzinfo=UTC), cost=7.0)

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: None, read_store=lambda runs_dir: None, read_reset=_sunday,
    )

    assert spend["weekly_fraction"] == 0.07
    assert spend["spend_source"] == "estimate"
    assert spend["weekly_source"] == "est"
    assert spend["spend_age_s"] is None


def test_estimate_without_a_configured_reset_keeps_the_rolling_week(tmp_path):
    _usage_file(tmp_path, "last-week", datetime(2026, 10, 3, 10, tzinfo=UTC), cost=40.0)

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: None, read_store=lambda runs_dir: None, read_reset=_no_reset,
    )

    assert spend["weekly_fraction"] == 0.40


def test_a_stale_meters_reset_anchors_the_weekly_estimate_over_the_configured_reset(tmp_path):
    # The stale meter says the week began Monday 10-05 04:00 UTC; the configured Sunday reset says 10-04 04:00.
    _usage_file(tmp_path, "sunday", datetime(2026, 10, 4, 10, tzinfo=UTC), cost=40.0)
    _usage_file(tmp_path, "tuesday", datetime(2026, 10, 6, 10, tzinfo=UTC), cost=7.0)
    stale = _meter(1, 2, NOW_DT - timedelta(days=1), weekly_resets_at=datetime(2026, 10, 12, 4, tzinfo=UTC))

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: stale, read_store=lambda runs_dir: None, read_reset=_sunday,
    )

    assert spend["weekly_fraction"] == 0.07
    assert spend["spend_source"] == "estimate"
    assert spend["spend_age_s"] == 86400


def test_the_newest_of_a_stale_local_and_a_stale_store_meter_anchors_the_week(tmp_path):
    _usage_file(tmp_path, "sunday", datetime(2026, 10, 4, 10, tzinfo=UTC), cost=40.0)
    _usage_file(tmp_path, "tuesday", datetime(2026, 10, 6, 10, tzinfo=UTC), cost=7.0)
    older_local = _meter(1, 2, NOW_DT - timedelta(days=2), weekly_resets_at=datetime(2026, 10, 11, 4, tzinfo=UTC))
    newer_store = _meter(1, 2, NOW_DT - timedelta(hours=1), weekly_resets_at=datetime(2026, 10, 12, 4, tzinfo=UTC))

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: older_local, read_store=lambda runs_dir: _doc(newer_store),
        read_reset=_no_reset,
    )

    assert spend["weekly_fraction"] == 0.07
    assert spend["spend_age_s"] == 3600


def test_with_no_meter_the_five_hour_estimate_is_the_last_five_hours_over_the_profile_ceiling(tmp_path, monkeypatch):
    implied = {"five_hour": 10.0, "weekly": None}
    monkeypatch.setattr(usage_meter, "implied_ceiling", lambda kind, now, *a, **k: implied[kind])
    _usage_file(tmp_path, "six-hours-ago", NOW_DT - timedelta(hours=6), cost=30.0)
    _usage_file(tmp_path, "one-hour-ago", NOW_DT - timedelta(hours=1), cost=5.0)

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: None, read_store=lambda runs_dir: None, read_reset=_sunday,
    )

    assert spend["five_hour_fraction"] == 0.05
    assert spend["five_hour_resets_at"] is None


def test_a_stale_meters_five_hour_reset_anchors_the_five_hour_estimate(tmp_path):
    reset = datetime(2026, 10, 9, 21, tzinfo=UTC)
    _usage_file(tmp_path, "before-reset", reset - timedelta(hours=1), cost=30.0)
    _usage_file(tmp_path, "after-reset", reset + timedelta(minutes=30), cost=12.0)
    stale = usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(used_percentage=0, resets_at=reset),
        seven_day=usage_meter.MeterEntry(used_percentage=2, resets_at=datetime(2026, 10, 11, 4, tzinfo=UTC)),
        observed_at=reset - timedelta(hours=2),
    )

    spend = dash_feed._spend(
        tmp_path, "2026-10-09T22:00:00Z",
        read_local=lambda: stale, read_store=lambda runs_dir: None, read_reset=_no_reset,
    )

    assert spend["five_hour_fraction"] == 0.12
    assert spend["five_hour_source"] == "est"


def test_a_meter_three_days_stale_leaves_the_five_hour_estimate_on_the_active_ccusage_block(tmp_path):
    for hours in range(1, 72, 5):
        _usage_file(tmp_path, f"host-a-{hours}", NOW_DT - timedelta(hours=hours), cost=4.0)
    block = {
        "isActive": True, "startTime": (NOW_DT - timedelta(hours=2)).isoformat(),
        "endTime": (NOW_DT + timedelta(hours=3)).isoformat(), "costUSD": 0.84,
    }
    (tmp_path / ".ccusage-block.json").write_text(json.dumps({"at": NOW_DT.isoformat(), "blocks": {"blocks": [block]}}))
    stale = usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(used_percentage=0, resets_at=NOW_DT - timedelta(hours=71)),
        seven_day=usage_meter.MeterEntry(used_percentage=2, resets_at=datetime(2026, 10, 11, 4, tzinfo=UTC)),
        observed_at=NOW_DT - timedelta(hours=73),
    )

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: stale, read_store=lambda runs_dir: None, read_reset=_no_reset,
    )

    assert spend["five_hour_fraction"] == pytest.approx(0.0084)
    assert spend["five_hour_source"] == "est"


@pytest.mark.parametrize(
    ("meter_age", "reset_age", "anchored"),
    [
        (timedelta(hours=4, minutes=59), timedelta(hours=71), True),
        (timedelta(hours=5), timedelta(hours=71), False),
        (timedelta(hours=73), timedelta(hours=71), False),
        (timedelta(hours=73), timedelta(hours=-2), False),
    ],
)
def test_the_five_hour_window_handed_to_gather_starts_under_five_hours_before_now_or_defers_to_ccusage(
    tmp_path, monkeypatch, meter_age, reset_age, anchored,
):
    seen: list[datetime | None] = []

    def recording_gather(runs_dir, now, *, ceiling_usd=None, start=None):
        seen.append(start)
        return SimpleNamespace(spent_usd=0.0, ceiling_usd=ceiling_usd)

    monkeypatch.setattr(dash_feed.usage_window, "gather", recording_gather)
    meter = usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(used_percentage=0, resets_at=NOW_DT - reset_age),
        seven_day=usage_meter.MeterEntry(used_percentage=2, resets_at=datetime(2026, 10, 11, 4, tzinfo=UTC)),
        observed_at=NOW_DT - meter_age,
    )

    dash_feed._spend(tmp_path, NOW, read_local=lambda: meter, read_store=lambda runs_dir: None, read_reset=_no_reset)

    (start,) = seen
    assert (start is not None) == anchored
    assert start is None or timedelta(0) <= NOW_DT - start < timedelta(hours=5)


def test_a_meter_just_under_five_hours_old_with_a_reset_days_gone_counts_from_a_window_under_five_hours_old(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(dash_feed, "_profile_ceilings", lambda: (60.0, 100.0))
    _usage_file(tmp_path, "host-b-old", NOW_DT - timedelta(hours=30), cost=50.0)
    _usage_file(tmp_path, "host-b-recent", NOW_DT - timedelta(minutes=30), cost=3.0)
    meter = usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(used_percentage=0, resets_at=NOW_DT - timedelta(hours=71)),
        seven_day=usage_meter.MeterEntry(used_percentage=2, resets_at=datetime(2026, 10, 11, 4, tzinfo=UTC)),
        observed_at=NOW_DT - timedelta(hours=4),
    )

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: meter, read_store=lambda runs_dir: None, read_reset=_no_reset,
    )

    assert spend["five_hour_fraction"] == pytest.approx(3.0 / 60.0)


def test_a_recorded_implied_five_hour_ceiling_does_not_change_the_five_hour_estimate(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_meter, "implied_ceiling", lambda kind, now, *a, **k: 10.0)
    _usage_file(tmp_path, "one-hour-ago", NOW_DT - timedelta(hours=1), cost=5.0)

    spend = dash_feed._spend(
        tmp_path, NOW, read_local=lambda: None, read_store=lambda runs_dir: None, read_reset=_no_reset,
    )

    assert spend["five_hour_fraction"] == 0.05


def test_v1_spend_keeps_every_existing_key_and_blanks_an_absent_age(tmp_path):
    spend = dash_feed._spend_v1(
        dash_feed._spend(tmp_path, NOW, read_local=lambda: None, read_store=lambda runs_dir: None, read_reset=_no_reset)
    )

    assert {
        "five_hour_fraction", "five_hour_source", "five_hour_resets_at", "weekly_fraction", "weekly_source",
        "weekly_resets_at", "hard_stop_fraction", "spend_source", "spend_age_s",
    } <= spend.keys()
    assert (spend["spend_source"], spend["spend_age_s"]) == ("estimate", "")


TODAY_TS = "2026-10-30T12:00:00+00:00"
TODAY_NOW = "2026-10-30T18:00:00Z"


def _landed(run: str, task: str) -> str:
    return json.dumps({"ts": TODAY_TS, "run": run, "task": task, "steps_reached": ["mark_done"], "exit": 0, "pr": None})


def _build(call_id: str, cost: float, task_id: str) -> dict:
    return {**ROW, "call_id": call_id, "ts": TODAY_TS, "cost_usd": cost, "role": "build", "task_id": task_id}


def test_the_feeds_today_and_the_dockets_efficiency_line_agree(tmp_path):
    store(
        tmp_path,
        _build("c0", 1.5, "epic-x-5:seams:seams-t"),
        _build("c1", 1.5, "epic-x-5:wires:wires-t"),
        _build("c2", 3.0, "epic-x-5:wires:wires-t"),
    )
    lines = [_landed("epic-x-5", t) for t in ("seams-task", "wires-task", "loose-task")]
    (tmp_path / "land.jsonl").write_text("\n".join(lines) + "\n")

    today = dash_feed._today_v1(tmp_path, TODAY_NOW)
    line = cli._efficiency(tmp_path, TODAY_NOW)

    assert today == {
        "date": "2026-10-30", "cost": 6.0, "landed": 3, "per_landed": 2.0, "first_try_rate": 0.5, "measured": 2,
    }
    assert line == "efficiency: today $6.00 for 3 landed ($2.00 per landed task), first-try 50% of 2 measured"
    assert line == route.efficiency_line(today["cost"], today["landed"], today["first_try_rate"], today["measured"])


def test_nothing_landed_gives_a_null_per_landed_and_first_try_rate(tmp_path):
    store(tmp_path, _build("c0", 0.5, "epic-x-5:seams:seams-t"))

    today = dash_feed._today_v1(tmp_path, TODAY_NOW)

    assert today == {
        "date": "2026-10-30", "cost": 0.5, "landed": 0, "per_landed": None, "first_try_rate": None, "measured": 0,
    }
    assert cli._efficiency(tmp_path, TODAY_NOW) == "efficiency: today $0.50, nothing landed yet"


def test_no_store_gives_a_null_today(tmp_path):
    assert dash_feed._today_v1(tmp_path, TODAY_NOW) is None
    assert cli._efficiency(tmp_path, TODAY_NOW) is None
