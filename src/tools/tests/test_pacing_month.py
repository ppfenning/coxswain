from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent_tools.budget import MonthBudget
from agent_tools.pacing import Policy, Window, assess

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
POLICY = Policy(
    pace_thresholds=(1.0, 1.2),
    tier_ladder=("opus", "sonnet"),
    effort_ladder=("high", "low"),
    min_headroom_usd=0.0,
)


def five_hour(spent: float = 0.0, ceiling: float | None = 10.0, burn: float = 0.0) -> Window:
    return Window(start=NOW - timedelta(hours=2.5), end=NOW + timedelta(hours=2.5), spent_usd=spent,
                  ceiling_usd=ceiling, burn_usd_per_hour=burn, runs_in_flight=0)


def week(spent: float = 0.0, ceiling: float | None = 1000.0) -> Window:
    return Window(start=NOW - timedelta(days=3.5), end=NOW + timedelta(days=3.5), spent_usd=spent,
                  ceiling_usd=ceiling, burn_usd_per_hour=0.0, runs_in_flight=0)


def month(spent: float, projected: float, verdict: str, budget: float | None = 100.0) -> MonthBudget:
    return MonthBudget(
        start=NOW - timedelta(days=15), end=NOW + timedelta(days=15), spent_usd=spent, budget_usd=budget,
        elapsed_fraction=0.5, daily_avg_usd=spent / 15, projected_usd=projected,
        fraction=spent / budget if budget else None, days_left=14, verdict=verdict,
    )


ON_TRACK = month(40.0, 80.0, "on_track")
OVER_PACE = month(50.0, 120.0, "over_pace")


def test_month_only_on_track_is_go() -> None:
    result = assess(five_hour(ceiling=None), POLICY, NOW, month=ON_TRACK)
    assert (result.verdict, result.tier_ceiling, result.effort_ceiling) == ("go", "opus", "high")
    assert (result.month_verdict, result.month_spent_usd, result.month_projected_usd) == ("on_track", 40.0, 80.0)
    assert "unmeasured" not in result.reason
    assert "month spent 40% of budget" in result.reason


def test_month_only_over_pace_is_rung_one() -> None:
    result = assess(five_hour(ceiling=None), POLICY, NOW, month=OVER_PACE)
    assert (result.verdict, result.tier_ceiling, result.effort_ceiling) == ("go_degraded", "sonnet", "high")
    assert result.month_verdict == "over_pace"


def test_month_only_over_budget_is_stop() -> None:
    lenient = replace(POLICY, weekly_hard_stop_fraction=1.5)
    result = assess(five_hour(ceiling=None), lenient, NOW, month=month(100.0, 200.0, "over_budget"))
    assert (result.verdict, result.tier_ceiling, result.effort_ceiling) == ("stop", "sonnet", "low")
    assert "unmeasured" not in result.reason


def test_month_hard_stop_at_exactly_the_fraction() -> None:
    at = assess(five_hour(ceiling=None), POLICY, NOW, month=month(93.0, 99.0, "on_track"))
    assert at.verdict == "stop"
    assert at.reason == "month spend 93% of monthly budget (hard stop at 93%)"
    below = assess(five_hour(ceiling=None), POLICY, NOW, month=month(92.0, 99.0, "on_track"))
    assert below.verdict == "go"


def test_month_stricter_than_a_measured_window() -> None:
    result = assess(five_hour(spent=1.0), POLICY, NOW, month=OVER_PACE)
    assert result.verdict == "go_degraded"
    assert result.reason.startswith("month spent 50% of budget")


def test_measured_weekly_alone_keeps_the_month_a_judge() -> None:
    result = assess(five_hour(ceiling=None), POLICY, NOW, weekly=week(spent=100.0), month=OVER_PACE)
    assert result.verdict == "go_degraded"


def test_window_stop_is_stricter_than_the_month() -> None:
    result = assess(five_hour(spent=10.0), POLICY, NOW, month=ON_TRACK)
    assert result.verdict == "stop"
    assert result.reason.startswith("spent 100% of ceiling")
    assert result.reason.endswith("; month 40% of monthly budget, on_track")
    assert (result.month_verdict, result.month_spent_usd, result.month_projected_usd) == ("on_track", 40.0, 80.0)


def test_window_hold_is_stricter_than_a_degraded_month() -> None:
    holding = replace(POLICY, min_headroom_usd=5.0)
    result = assess(five_hour(spent=1.0, burn=2.0), holding, NOW, month=OVER_PACE)
    assert result.verdict == "hold"
    assert "month 50% of monthly budget, over_pace" in result.reason


def test_month_stop_is_stricter_than_a_window_hold() -> None:
    holding = replace(POLICY, min_headroom_usd=5.0)
    result = assess(five_hour(spent=1.0, burn=2.0), holding, NOW, month=month(95.0, 99.0, "on_track"))
    assert result.verdict == "stop"


def test_tie_keeps_the_window_result_and_narrates_the_month() -> None:
    without = assess(five_hour(spent=1.0), POLICY, NOW)
    result = assess(five_hour(spent=1.0), POLICY, NOW, month=ON_TRACK)
    assert result.verdict == without.verdict == "go"
    assert result.reason == f"{without.reason}; month 40% of monthly budget, on_track"


def test_no_month_leaves_the_result_unchanged() -> None:
    for window, weekly in [(five_hour(spent=1.0), None), (five_hour(ceiling=None), None),
                           (five_hour(spent=1.0), week(spent=100.0))]:
        today = assess(window, POLICY, NOW, weekly)
        assert assess(window, POLICY, NOW, weekly, month=None) == today
        assert assess(window, POLICY, NOW, weekly, month=month(50.0, 120.0, "unmeasured", budget=None)) == today
        assert assess(window, POLICY, NOW, weekly, month=month(50.0, 120.0, "unmeasured", budget=0.0)) == today
        assert (today.month_verdict, today.month_spent_usd, today.month_projected_usd) == (None, None, None)
