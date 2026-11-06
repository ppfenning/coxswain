from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agent_tools.dash_live_series import (
    cache_hit_pct,
    calls_per_min,
    per_hour,
    throughput,
)

NOW = datetime(2026, 11, 6, 12, 30, 30, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_board_v1.json"


def turn(ts, host, i=0, o=0, r=0, w=0):
    return {
        "ts": ts,
        "host": host,
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_tokens": r,
        "cache_write_tokens": w,
        "model": "m",
    }


TURNS = [
    turn("2026-11-06T12:30:05Z", "host-b", 100, 10, 300, 0),
    turn("2026-11-06T12:30:45Z", "host-b", 50, 5, 150, 0),
    turn("2026-11-06T12:30:10Z", "host-a", 20, 2, 60, 20),
    turn("2026-11-06T12:00:00Z", "host-a", 7, 3, 1, 0),
    turn("2026-11-06T11:29:45Z", "host-c", 999, 999, 999, 999),
]

ZERO = {"input": 0, "output": 0, "cache_read": 0}


def test_window_is_sixty_minutes_ending_at_now():
    system = throughput(TURNS, NOW)["system"]
    assert len(system) == 60
    assert system[0]["at"] == "2026-11-06T11:31:00Z"
    assert system[-1]["at"] == "2026-11-06T12:30:00Z"


def test_system_tokens_per_minute():
    system = {p["at"]: p for p in throughput(TURNS, NOW)["system"]}
    assert system["2026-11-06T12:30:00Z"] == {
        "at": "2026-11-06T12:30:00Z",
        "input": 170,
        "output": 17,
        "cache_read": 510,
    }
    assert system["2026-11-06T12:00:00Z"] == {
        "at": "2026-11-06T12:00:00Z",
        "input": 7,
        "output": 3,
        "cache_read": 1,
    }


def test_host_series_sorted_and_stale_host_omitted():
    hosts = throughput(TURNS, NOW)["hosts"]
    assert [h["host"] for h in hosts] == ["host-a", "host-b"]
    last_b = hosts[1]["series"][-1]
    assert last_b == {
        "at": "2026-11-06T12:30:00Z",
        "input": 150,
        "output": 15,
        "cache_read": 450,
    }
    assert len(hosts[0]["series"]) == 60


def test_turn_at_61_minutes_is_dropped_and_first_minute_is_kept():
    kept = turn("2026-11-06T11:31:59Z", "host-a", 4, 0, 0, 0)
    out = throughput([TURNS[-1], kept], NOW)
    assert out["system"][0] == {"at": "2026-11-06T11:31:00Z", "input": 4, "output": 0, "cache_read": 0}
    assert sum(p["input"] for p in out["system"]) == 4
    assert [h["host"] for h in out["hosts"]] == ["host-a"]


def test_empty_minutes_are_zero():
    system = throughput([], NOW)
    assert system["hosts"] == []
    assert all({k: p[k] for k in ZERO} == ZERO for p in system["system"])


def test_calls_per_min_counts_turns():
    points = {p["at"]: p["value"] for p in calls_per_min(TURNS, NOW)}
    assert len(points) == 60
    assert points["2026-11-06T12:30:00Z"] == 3
    assert points["2026-11-06T12:00:00Z"] == 1
    assert points["2026-11-06T12:29:00Z"] == 0
    assert sum(points.values()) == 4


def test_cache_hit_pct_per_minute():
    points = {p["at"]: p["value"] for p in cache_hit_pct(TURNS, NOW)}
    assert len(points) == 60
    # 100 * 510 / (170 + 510 + 20) = 72.857...
    assert points["2026-11-06T12:30:00Z"] == 72.9
    # 100 * 1 / (7 + 1 + 0) = 12.5
    assert points["2026-11-06T12:00:00Z"] == 12.5
    assert points["2026-11-06T12:29:00Z"] == 0.0


def test_cache_hit_pct_exact_ratio():
    only = [turn("2026-11-06T12:30:00Z", "host-a", 60, 0, 30, 10)]
    assert cache_hit_pct(only, NOW)[-1]["value"] == 30.0


def test_per_hour_buckets_events_oldest_first():
    events = [
        {"ts": "2026-11-06T12:05:00Z"},
        {"ts": "2026-11-06T12:59:59Z"},
        {"ts": "2026-11-06T10:00:00Z"},
        {"ts": "2026-11-05T13:00:00Z"},
        {"ts": "2026-11-05T12:59:59Z"},
        {"ts": "2026-11-06T13:00:00Z"},
    ]
    points = per_hour(events, NOW, 24)
    assert len(points) == 24
    assert points[0] == {"at": "2026-11-05T13:00:00Z", "value": 1}
    assert points[-1] == {"at": "2026-11-06T12:00:00Z", "value": 2}
    assert points[-3] == {"at": "2026-11-06T10:00:00Z", "value": 1}
    assert sum(p["value"] for p in points) == 4


def test_point_keys_match_the_feed_fixture():
    live = json.loads(FIXTURE.read_text())["live"]
    assert set(throughput(TURNS, NOW)["system"][0]) == set(live["throughput"]["system"][0])
    assert set(throughput(TURNS, NOW)["hosts"][0]) == set(live["throughput"]["hosts"][0])
    assert set(calls_per_min(TURNS, NOW)[0]) == set(live["calls_per_min"][0])
    assert set(cache_hit_pct(TURNS, NOW)[0]) == set(live["cache_hit_pct"][0])
    assert set(per_hour([], NOW, 24)[0]) == set(live["lands_per_hour"][0])
    assert calls_per_min(TURNS, NOW)[0]["at"][-1] == "Z"
    assert live["calls_per_min"][0]["at"][-1] == "Z"
