from __future__ import annotations

import json
from pathlib import Path

from agent_tools.operator_sessions import session_rows, sources, window_costs, with_holder

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_sessions_v1.json").read_text())

# One input token is a cent: 421 tokens price to $4.21.
_PRICES = {
    "claude-opus-5": {"input": 10_000.0},
    "claude-sonnet-5-5": {"input": 10_000.0},
}
_WINDOWS = {
    "five_hour": {"start": "2026-11-06T17:00:00Z"},
    "weekly": {"start": "2026-11-01T08:00:00Z"},
}


def _line(ts: str, mid: str, tokens: int, model: str = "claude-opus-5") -> str:
    message = {"id": mid, "model": model, "usage": {"input_tokens": tokens}}
    return json.dumps({"type": "assistant", "timestamp": ts, "message": message})


def _agent(agent_id: str, meta: dict, lines: list[str]) -> dict:
    return {"agent_id": agent_id, "meta": meta, "lines": lines}


_OPUS = {
    "session_id": "5f0c2a9e-3b1d-4c7a-9e21-0a6b7d8c1f34",
    "lines": [
        _line("2026-11-01T08:15:30.250Z", "o1", 819),
        _line("2026-11-06T18:00:00Z", "o2", 421),
    ],
    "agents": [
        _agent(
            "a1",
            {"agentType": "general-purpose", "description": "Survey the dash feed readers"},
            [_line("2026-11-03T09:00:00Z", "a1x", 28), _line("2026-11-06T18:30:00Z", "a1y", 62)],
        ),
        _agent(
            "a2",
            {"agentType": "Explore", "description": "Find the spend split callers"},
            [_line("2026-11-06T18:40:00Z", "a2x", 18)],
        ),
        _agent("a3", {"agentType": "Plan", "description": "Too early"}, [_line("2026-10-20T09:00:00Z", "a3x", 5)]),
    ],
}
_SONNET = {
    "session_id": "c81e4d57-92a0-4f6b-b3d5-71e2a9f04c68",
    "lines": [_line("2026-11-05T14:02:11Z", "s1", 3000, "claude-sonnet-5-5")],
    "agents": [
        _agent(
            "b1",
            {"agentType": "general-purpose", "description": "Review the lease reader"},
            [_line("2026-11-05T15:00:00Z", "b1x", 160, "claude-sonnet-5-5")],
        )
    ],
}
_SESSIONS = [_OPUS, _SONNET]


def test_rows_equal_the_fixture():
    assert session_rows(_SESSIONS, _PRICES, _WINDOWS) == _FIXTURE["operator_sessions"]


def test_window_costs_are_the_unrounded_split_inputs():
    five = window_costs(_SESSIONS, _PRICES, "2026-11-06")
    weekly = window_costs(_SESSIONS, _PRICES, "2026-11-01")
    assert [round(v, 4) for v in five] == [4.21, 0.8]
    assert [round(v, 4) for v in weekly] == [42.4, 2.68]
    assert all(isinstance(v, float) for v in (*five, *weekly))


def test_sources_match_the_fixture():
    expected = _FIXTURE["spend_sources"]
    assert sources(_SESSIONS, _PRICES) == {
        "sessions_found": expected["sessions_found"],
        "sessions_priced": expected["sessions_priced"],
    }


def test_no_sessions_found_and_none_priced():
    assert sources([], _PRICES) == {"sessions_found": 0, "sessions_priced": "none"}
    assert session_rows([], _PRICES, _WINDOWS) == {"five_hour": [], "weekly": []}
    assert window_costs([], _PRICES, "2026-11-06") == (0.0, 0.0)


def test_a_missing_catalog_finds_sessions_and_prices_none():
    assert sources(_SESSIONS, {}) == {"sessions_found": 2, "sessions_priced": "none"}
    rows = session_rows(_SESSIONS, {}, _WINDOWS)
    assert [r["cost_usd"] for r in rows["weekly"]] == [0.0, 0.0]


def test_a_session_without_a_parsable_timestamp_is_dropped():
    bare = {"session_id": "x", "lines": ["not json", json.dumps({"type": "user"})], "agents": []}
    assert session_rows([bare], _PRICES, _WINDOWS) == {"five_hour": [], "weekly": []}


def test_agent_type_falls_back_and_description_is_first_line_capped():
    stamp = "2026-11-06T18:00:00Z"
    agents = [
        _agent("p", {"subagent_type": "Explore", "description": "d" * 150 + "\nsecond"}, [_line(stamp, "p1", 3)]),
        _agent("q", {}, [_line(stamp, "q1", 2)]),
    ]
    session = {"session_id": "z", "lines": [_line(stamp, "z1", 1)], "agents": agents}
    row = session_rows([session], _PRICES, _WINDOWS)["five_hour"][0]
    assert row["agents"] == [
        {"agent_type": "Explore", "description": "d" * 120, "cost_usd": 0.03},
        {"agent_type": "", "description": "", "cost_usd": 0.02},
    ]


def test_with_holder_appends_when_no_session_starts_with_it():
    sessions = [dict(_SONNET)]
    out = with_holder(sessions, "5f0c", ["l"], [])
    assert [s["session_id"] for s in out] == [_SONNET["session_id"], "5f0c"]
    assert out[1] == {"session_id": "5f0c", "lines": ["l"], "agents": []}
    assert len(sessions) == 1


def test_with_holder_leaves_the_sessions_when_present_or_empty():
    assert with_holder(_SESSIONS, "5f0c", ["l"], []) == _SESSIONS
    assert with_holder(_SESSIONS, "", ["l"], []) == _SESSIONS
    assert with_holder(_SESSIONS, "5f0c", ["l"], []) is not _SESSIONS
