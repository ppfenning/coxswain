import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_tools.dash_live_agents import agents, default_context_windows

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
WINDOWS = {"claude-sonnet-5-5": 200_000, "claude-opus-4-7": 1_000_000}


def turn(ago_s=10, **over):
    base = {
        "ts": (NOW - timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": "host-b",
        "source": "lane",
        "kind": "build",
        "agent_id": "a1",
        "model": "claude-sonnet-5-5",
        "run": "r1",
        "initiative": "",
        "task": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.0,
    }
    return {**base, **over}


def test_chair_subagent_lane_node_are_listed_and_the_idle_agent_is_not():
    turns = [
        turn(60, source="session", kind="chair", agent_id="chair@host-b:1", model="claude-opus-4-7", input_tokens=500),
        turn(30, source="subagent", kind="subagent", agent_id="agent-7f3a", task="board-feed", input_tokens=200),
        turn(20, source="lane", kind="build", agent_id="board-feed-1-build-1", task="board-render", input_tokens=300),
        turn(601, source="session", kind="operator", agent_id="operator@host-b:2", input_tokens=9999),
    ]
    rows = agents(turns, NOW, WINDOWS)
    assert [r["id"] for r in rows] == ["chair@host-b:1", "board-feed-1-build-1", "agent-7f3a"]
    assert [r["kind"] for r in rows] == ["chair", "build", "subagent"]


def test_cutoff_is_inclusive_at_live_within_s():
    assert [r["id"] for r in agents([turn(600)], NOW, WINDOWS)] == ["a1"]
    assert agents([turn(601)], NOW, WINDOWS) == []
    assert agents([turn(601)], NOW, WINDOWS, live_within_s=700) != []


def test_row_takes_kind_model_host_from_the_newest_turn_and_elapsed_from_the_oldest():
    turns = [
        turn(120, kind="build", model="claude-sonnet-5-5", host="host-a"),
        turn(5, kind="review", model="claude-opus-4-7", host="host-b"),
    ]
    (row,) = agents(turns, NOW, WINDOWS)
    assert (row["kind"], row["model"], row["host"], row["elapsed_s"]) == ("review", "claude-opus-4-7", "host-b", 120)


def test_serves_is_task_then_initiative_then_empty():
    assert agents([turn(task="t", initiative="i")], NOW, WINDOWS)[0]["serves"] == "t"
    assert agents([turn(task="", initiative="i")], NOW, WINDOWS)[0]["serves"] == "i"
    assert agents([turn()], NOW, WINDOWS)[0]["serves"] == ""
    newest_wins = [turn(50, task="old"), turn(5, task="", initiative="i")]
    assert agents(newest_wins, NOW, WINDOWS)[0]["serves"] == "i"


def test_rate_counts_last_five_minutes_of_input_output_cache_read_only_cost_counts_all_turns():
    turns = [
        turn(400, input_tokens=100_000, cost_usd=1.00001),
        turn(200, input_tokens=100, output_tokens=200, cache_read_tokens=300, cache_write_tokens=7777, cost_usd=0.25),
        turn(10, input_tokens=1, output_tokens=2, cache_read_tokens=3, cost_usd=0.125),
    ]
    (row,) = agents(turns, NOW, WINDOWS)
    assert row["tokens_per_min"] == 121
    assert row["cost_usd"] == 1.3750


def test_context_fill_known_model_unknown_model_and_cap():
    known = turn(input_tokens=20_000, cache_read_tokens=60_000, cache_write_tokens=2_000, output_tokens=99_999)
    assert agents([known], NOW, WINDOWS)[0]["context_fill"] == 0.41
    assert agents([{**known, "model": "mystery-1"}], NOW, WINDOWS)[0]["context_fill"] == 0.0
    over = turn(input_tokens=150_000, cache_read_tokens=150_000)
    assert agents([over], NOW, WINDOWS)[0]["context_fill"] == 1.0


def test_context_fill_reads_the_newest_turn_and_rounds_to_three_places():
    turns = [turn(100, input_tokens=190_000), turn(5, input_tokens=1_000, cache_write_tokens=234)]
    assert agents(turns, NOW, WINDOWS)[0]["context_fill"] == 0.006


def test_rows_sort_by_rate_descending_then_id():
    turns = [
        turn(agent_id="b", input_tokens=500),
        turn(agent_id="a", input_tokens=500),
        turn(agent_id="z", input_tokens=5000),
        turn(agent_id="idle-rate", input_tokens=0),
    ]
    assert [r["id"] for r in agents(turns, NOW, WINDOWS)] == ["z", "a", "b", "idle-rate"]


def test_same_agent_id_under_two_sources_is_two_agents():
    turns = [turn(source="lane", agent_id="x"), turn(source="subagent", agent_id="x")]
    assert len(agents(turns, NOW, WINDOWS)) == 2


def test_now_may_be_an_iso_string_and_input_turns_are_not_changed():
    turns = [turn(input_tokens=10)]
    before = [dict(t) for t in turns]
    assert agents(turns, "2026-11-06T12:00:00Z", WINDOWS) == agents(turns, NOW, WINDOWS)
    assert turns == before


def test_no_turns_gives_no_agents():
    assert agents([], NOW, WINDOWS) == []


def test_default_windows_prefer_the_catalog_field_and_alias_names():
    catalog = {
        "models": [
            {"id": "claude-opus-5-5", "aliases": ["opus"], "context_window": 1_000_000},
            {"id": "claude-sonnet-5-5", "aliases": ["sonnet"], "context_window": None},
        ]
    }
    windows = default_context_windows(catalog)
    assert (windows["claude-opus-5-5"], windows["opus"]) == (1_000_000, 1_000_000)
    assert "claude-sonnet-5-5" not in windows


def test_default_windows_without_a_catalog_holds_only_the_sourced_figure():
    expected = {"claude-haiku-4-5-20251001": 200_000, "claude-haiku-4-5": 200_000, "haiku": 200_000}
    assert default_context_windows(None) == expected
    assert default_context_windows({}) == expected


def test_a_model_with_no_sourced_window_has_fill_zero_not_an_invented_one():
    windows = default_context_windows(None)
    assert agents([turn(input_tokens=50_000)], NOW, windows)[0]["context_fill"] == 0.0
    assert agents([turn(model="haiku", input_tokens=50_000)], NOW, windows)[0]["context_fill"] == 0.25


def test_zero_cost_agent_has_float_cost():
    (row,) = agents([turn(cost_usd=0.0), turn(5, cost_usd=0.0)], NOW, WINDOWS)
    assert type(row["cost_usd"]) is float and row["cost_usd"] == 0.0


def test_tokens_per_min_is_an_int_even_when_idle():
    (row,) = agents([turn()], NOW, WINDOWS)
    assert type(row["tokens_per_min"]) is int and row["tokens_per_min"] == 0


def test_a_turn_stamped_after_now_counts_as_age_zero():
    turns = [turn(-30, input_tokens=300), turn(-90, input_tokens=300)]
    (row,) = agents(turns, NOW, WINDOWS)
    assert (row["tokens_per_min"], row["elapsed_s"]) == (120, 0)


FEED_FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_v1.json"
FIXTURE_WINDOWS = {"claude-opus-4-7": 1_000_000, "claude-sonnet-5-5": 200_000, "claude-haiku-4-5": 200_000}


def fixture_agents():
    return json.loads(FEED_FIXTURE.read_text())["live"]["agents"]


def turns_for(row):
    """An oldest silent turn fixes elapsed_s; the newest carries the rate, cost and context fill of the fixture row."""
    window = FIXTURE_WINDOWS[row["model"]]
    rate_tokens = row["tokens_per_min"] * 5
    fill_tokens = round(row["context_fill"] * window)
    common = {
        "agent_id": row["id"],
        "kind": row["kind"],
        "model": row["model"],
        "host": row["host"],
        "task": row["serves"],
    }
    return [
        turn(row["elapsed_s"], **common),
        turn(
            1,
            input_tokens=rate_tokens,
            cache_write_tokens=fill_tokens - rate_tokens,
            cost_usd=row["cost_usd"],
            **common,
        ),
    ]


def fixture_turns():
    return [t for row in fixture_agents() for t in turns_for(row)]


def test_agents_equal_the_committed_live_agents_fixture():
    assert agents(fixture_turns(), NOW, FIXTURE_WINDOWS) == fixture_agents()


def test_every_fixture_tokens_per_min_is_an_int_and_the_idle_agent_is_zero():
    rows = agents(fixture_turns(), NOW, FIXTURE_WINDOWS)
    assert all(type(r["tokens_per_min"]) is int for r in rows)
    (idle,) = [r for r in rows if r["kind"] == "operator"]
    assert type(idle["tokens_per_min"]) is int and idle["tokens_per_min"] == 0


@pytest.mark.parametrize("tokens, expected", [(301, 60), (297, 59), (3, 1), (1, 0)])
def test_a_fractional_rate_rounds_to_the_nearest_whole_token(tokens, expected):
    (row,) = agents([turn(input_tokens=tokens)], NOW, WINDOWS)
    assert type(row["tokens_per_min"]) is int and row["tokens_per_min"] == expected
