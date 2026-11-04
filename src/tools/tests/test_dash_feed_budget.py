import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools import budget, dash_feed, usage_window

_FIXTURES = Path(__file__).parent / "fixtures"
# The contract's worked case: 9.5 days into a 30 day month, 190.0 spent, 20 days left, a 600.0 projection.
_AT = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_NOW = "2026-11-10T12:00:00Z"
_TODAY = date(2026, 11, 10)


def _contract() -> dict:
    return json.loads((_FIXTURES / "dash_budget_v1.json").read_text())


def _store(runs_dir, since: str, until: str | None = None) -> float | None:
    """A store that holds 190.0 for the month to date, 21.5 for each of the last seven days, 17.0 for the seven before."""
    if until == _AT.isoformat() and since.startswith("2026-11-01"):
        return 190.0
    day = datetime.fromisoformat(since).date()
    if day > _TODAY or day <= _TODAY - timedelta(days=14):
        return None
    return 21.5 if day > _TODAY - timedelta(days=7) else 17.0


def _wire(monkeypatch, limit: float | None) -> None:
    monkeypatch.setattr(dash_feed, "_profile_budget", lambda: (limit, "UTC"))
    monkeypatch.setattr(dash_feed, "_pacing_policy", lambda runs_dir: SimpleNamespace(min_elapsed_fraction=0.1))
    monkeypatch.setattr(dash_feed.run_store, "cost_since", _store)


def _no_nulls(value) -> bool:
    if isinstance(value, dict):
        return all(_no_nulls(item) for item in value.values())
    return value is not None


def test_a_budget_set_builds_the_contract_month_case(monkeypatch, tmp_path):
    _wire(monkeypatch, 400.0)

    assert dash_feed._budget_edge(tmp_path, _AT) == _contract()["month"]


def test_no_budget_set_builds_the_contract_none_case(monkeypatch, tmp_path):
    _wire(monkeypatch, None)

    assert dash_feed._budget_edge(tmp_path, _AT) == _contract()["none"]


def test_no_value_is_null(monkeypatch, tmp_path):
    for limit in (400.0, None):
        _wire(monkeypatch, limit)
        assert _no_nulls(dash_feed._budget_edge(tmp_path, _AT))
    assert _no_nulls(dash_feed.snapshot("t", {}, {}, [], [], [], 0, [], 0, [])["budget"])


def test_the_snapshot_defaults_to_the_none_block_with_every_figure_zero():
    block = dash_feed.snapshot("t", {}, {}, [], [], [], 0, [], 0, [])["budget"]

    assert block == {**dict.fromkeys(_contract()["none"], 0), "period": "none"}


def test_money_rounds_to_cents_and_the_fraction_to_four_places():
    month = budget.MonthBudget(
        start=_AT, end=_AT, spent_usd=190.004, budget_usd=400.0, elapsed_fraction=0.3,
        daily_avg_usd=20.006, projected_usd=600.0049, fraction=0.47512, days_left=20, verdict="on_track",
    )

    assert dash_feed._budget_v1(month, 21.504, 19.2549) == {
        "period": "month", "spent_usd": 190.0, "budget_usd": 400.0, "fraction": 0.4751, "daily_avg_usd": 20.01,
        "projected_usd": 600.0, "days_left": 20, "daily_avg_7d_usd": 21.5, "daily_avg_14d_usd": 19.25,
    }


def test_a_failing_read_gives_the_none_block_not_a_dead_feed(monkeypatch, tmp_path):
    _wire(monkeypatch, 400.0)

    def broken(*args, **kwargs):
        raise OSError("usage files unreadable")

    monkeypatch.setattr(usage_window, "gather_month", broken)

    assert dash_feed._budget_edge(tmp_path, _AT) == {**dict.fromkeys(_contract()["none"], 0.0), "period": "none", "days_left": 0}


def _profile(monkeypatch, tmp_path, text: str) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(text)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(path))


def test_the_profile_reader_prefers_budget_timezone(monkeypatch, tmp_path):
    _profile(monkeypatch, tmp_path, "spend:\n  monthly_budget_usd: 400\n  budget_timezone: Asia/Tokyo\n  weekly_reset: Sun 04:00 America/New_York\n")

    assert dash_feed._profile_budget() == (400.0, "Asia/Tokyo")


def test_the_profile_reader_falls_back_to_the_weekly_reset_zone(monkeypatch, tmp_path):
    _profile(monkeypatch, tmp_path, "spend:\n  monthly_budget_usd: 400\n  weekly_reset: Sun 04:00 America/New_York\n")

    assert dash_feed._profile_budget() == (400.0, "America/New_York")


def test_the_profile_reader_falls_back_to_utc(monkeypatch, tmp_path):
    _profile(monkeypatch, tmp_path, "spend:\n  window_ceiling_usd: 40\n")
    assert dash_feed._profile_budget() == (None, "UTC")

    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path / "missing.yaml"))
    assert dash_feed._profile_budget() == (None, "UTC")


@pytest.fixture
def _bare_feed(monkeypatch):
    """`gather_feed` with every section but the budget reduced to nothing, so only the budget wiring is under test."""
    sections = {"chair": [], "spend": {}, "hosts": [], "lanes": []}
    monkeypatch.setattr(dash_feed.console_screen, "gather", lambda *args, **kwargs: sections)
    monkeypatch.setattr(dash_feed, "_spend", lambda *args: {})
    monkeypatch.setattr(
        dash_feed, "_activity", lambda *args: {"chair_agents": [], "spend_split": None, "spend_sources": None},
    )
    monkeypatch.setattr(dash_feed, "_local_identity", lambda runs_dir, profile: ("host-b", 3))
    monkeypatch.setattr(dash_feed, "_local_login", lambda local_name: None)
    monkeypatch.setattr(dash_feed, "_queue", lambda runs_dir, live_runs=(): ([], 0))
    monkeypatch.setattr(dash_feed, "_inbox", lambda entries: ([], 0))
    monkeypatch.setattr(dash_feed.run_store, "initiative_short_ids", lambda runs_dir: {})
    monkeypatch.setattr(dash_feed, "_pacing_policy", lambda runs_dir: SimpleNamespace(min_elapsed_fraction=0.1))
    monkeypatch.setattr(dash_feed.run_store, "cost_since", _store)


def test_gather_feed_carries_the_budget_block_the_store_and_profile_give(_bare_feed, monkeypatch, tmp_path):
    _profile(monkeypatch, tmp_path, "spend:\n  monthly_budget_usd: 400\n")

    feed = json.loads(json.dumps(dash_feed.gather_feed(tmp_path, tmp_path, _NOW)))

    assert feed["budget"] == _contract()["month"]


def test_gather_feed_with_no_limit_carries_spend_and_averages_under_period_none(_bare_feed, monkeypatch, tmp_path):
    _profile(monkeypatch, tmp_path, "spend:\n  window_ceiling_usd: 40\n")

    feed = json.loads(json.dumps(dash_feed.gather_feed(tmp_path, tmp_path, _NOW)))

    assert feed["budget"] == _contract()["none"]
