import json
from datetime import UTC, datetime
from pathlib import Path

from agent_tools.dash_live_burn import burn

FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_board_v1.json"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
TURNS = [
    {"ts": "2026-11-06T11:58:00Z", "cost_usd": 0.30},
    {"ts": "2026-11-06T11:56:00Z", "cost_usd": 0.20},
    {"ts": "2026-11-06T11:54:59Z", "cost_usd": 0.50},
    {"ts": "2026-11-06T11:44:00Z", "cost_usd": 9.00},
]
FIVE = {"used_fraction": 0.2, "window_spend_usd": 2.0, "resets_at": "2026-11-06T12:30:00Z", "ceiling_fraction": 0.93}
WEEK = {"used_fraction": 0.5, "window_spend_usd": 4.0, "resets_at": "2026-11-06T13:00:00Z", "ceiling_fraction": 0.93}


def meters(**week: object) -> dict:
    return {"five_hour": FIVE, "weekly": {**WEEK, **week}}


def test_rate_over_each_window() -> None:
    result = burn(TURNS, meters(), NOW)
    assert result["usd_per_min_5m"] == 0.1
    assert result["usd_per_min_15m"] == 0.0667


def test_turn_just_outside_five_minutes_counts_only_in_fifteen() -> None:
    result = burn([{"ts": "2026-11-06T11:54:59Z", "cost_usd": 0.5}], meters(), NOW)
    assert (result["usd_per_min_5m"], result["usd_per_min_15m"]) == (0.0, 0.0333)


def test_turn_exactly_five_minutes_old_is_outside_the_window() -> None:
    assert burn([{"ts": "2026-11-06T11:55:00Z", "cost_usd": 0.5}], meters(), NOW)["usd_per_min_5m"] == 0.0


def test_projection_under_ceiling() -> None:
    # spend 2.0 + 0.1 USD/min * 30 min = 5.0; 0.2 * 5.0 / 2.0 = 0.5
    assert burn(TURNS, meters(), NOW)["meters"]["five_hour"] == {
        "used_fraction": 0.2,
        "projected_fraction": 0.5,
        "ceiling_fraction": 0.93,
        "resets_at": "2026-11-06T12:30:00Z",
        "over_ceiling": False,
    }


def test_projection_over_ceiling() -> None:
    # spend 4.0 + 0.1 USD/min * 60 min = 10.0; 0.5 * 10.0 / 4.0 = 1.25
    weekly = burn(TURNS, meters(), NOW)["meters"]["weekly"]
    assert (weekly["projected_fraction"], weekly["over_ceiling"]) == (1.25, True)


def test_past_resets_at_projects_no_growth() -> None:
    weekly = burn(TURNS, meters(resets_at="2026-11-06T11:00:00Z"), NOW)["meters"]["weekly"]
    assert (weekly["projected_fraction"], weekly["over_ceiling"]) == (0.5, False)


def test_empty_resets_at_projects_no_growth() -> None:
    weekly = burn(TURNS, meters(resets_at=""), NOW)["meters"]["weekly"]
    assert (weekly["projected_fraction"], weekly["resets_at"]) == (0.5, "")


def test_zero_window_spend_projects_used_fraction() -> None:
    weekly = burn(TURNS, meters(window_spend_usd=0, used_fraction=0.3), NOW)["meters"]["weekly"]
    assert (weekly["projected_fraction"], weekly["over_ceiling"]) == (0.3, False)


def test_zero_ceiling_is_never_over() -> None:
    weekly = burn(TURNS, meters(ceiling_fraction=0.0), NOW)["meters"]["weekly"]
    assert (weekly["projected_fraction"], weekly["over_ceiling"]) == (1.25, False)


def test_no_turns_gives_zero_rates() -> None:
    result = burn([], meters(), NOW)
    assert (result["usd_per_min_5m"], result["usd_per_min_15m"]) == (0.0, 0.0)


def test_shape_matches_the_fixture() -> None:
    expected = json.loads(FIXTURE.read_text())["live"]["burn"]
    result = burn(TURNS, meters(), NOW)
    assert set(result) == set(expected)
    assert set(result["meters"]) == set(expected["meters"])
    for name, meter in expected["meters"].items():
        assert set(result["meters"][name]) == set(meter)
