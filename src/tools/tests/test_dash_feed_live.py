import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import console_screen, dash_feed

NOW = datetime(2026, 11, 6, 12, 0, 30, tzinfo=UTC)


def _turn(minute, host, agent, tokens, cost, source="lane", model="claude-sonnet-5-5"):
    return {
        "ts": f"2026-11-06T11:{minute:02d}:10Z", "host": host, "source": source, "agent_id": agent, "model": model,
        "kind": "chair" if source == "session" else "build",
        "input_tokens": tokens, "output_tokens": 0, "cache_read_tokens": tokens * 3, "cache_write_tokens": 0,
        "cost_usd": cost,
    }


def _keys(value):
    """The key structure of a JSON value: dict keys at every level, a list by its first item."""
    if isinstance(value, dict):
        return {key: _keys(item) for key, item in value.items()}
    return [_keys(value[0])] if isinstance(value, list) and value else None


def _lane(run, host, phase="p1"):
    return console_screen.LaneRow(run, host, "", phase, "build", 1, 3, 0.5, 0, 2)


def _stub_turns(monkeypatch, by_run, session=()):
    monkeypatch.setattr(dash_feed.chair_activity_read, "prices_for_profile", lambda profile: {})
    monkeypatch.setattr(
        dash_feed.dash_live_turns_lane, "read_lane_turns",
        lambda runs_dir, run, host, initiative, task, node, prices=None: by_run[run],
    )
    monkeypatch.setattr(dash_feed.dash_live_turns_session, "read_session_turns", lambda *a, **k: list(session))


def _edge(tmp_path, targets, ceiling=0.93, events=(), profile=None):
    meters = dash_feed._burn_meters(
        {"five_hour_fraction": 0.4, "five_hour_resets_at": "2026-11-06T14:00:00Z",
         "weekly_fraction": 0.5, "weekly_resets_at": "2026-11-08T04:00:00Z"},
        {"five_hour": {"cox_seats_usd": 4.0}, "weekly": {"cox_seats_usd": 5.0}}, ceiling,
    )
    return dash_feed._live_edge(tmp_path, targets, meters, events, profile or {}, "host-a", "chair123", NOW)


def _targets():
    lanes = [_lane("alpha-1", "host-a"), _lane("beta-1", "host-b")]
    return dash_feed._lane_targets(lanes, {("beta", "p1", "t-b"): "building"}, [], {}, "host-a", NOW)


def test_targets_read_each_lane_by_host_initiative_task_and_node_and_recent_runs_only():
    records = [
        {"run": "gamma-1", "node": "review", "ended_at": "2026-11-06T11:30:00Z"},
        {"run": "delta-1", "node": "build", "ended_at": "2026-11-06T11:40:00Z"},
        {"run": "old-1", "node": "review", "ended_at": "2026-11-06T09:00:00Z"},
    ]
    lanes = [_lane("alpha-1", None), _lane("beta-1", "host-b")]
    hosts = {"delta-1": "host-c"}  # the store names the host of a finished run; gamma-1 has none and is local
    targets = dash_feed._lane_targets(lanes, {("beta", "p1", "t-b"): "building"}, records, hosts, "host-a", NOW)

    assert [(t["run"], t["host"], t["initiative"], t["task"], t["node"]) for t in targets] == [
        ("alpha-1", "host-a", "alpha", "", "build"), ("beta-1", "host-b", "beta", "t-b", "build"),
        ("gamma-1", "host-a", "gamma", "", "review"), ("delta-1", "host-c", "delta", "", "build"),
    ]


def test_two_initiatives_on_two_hosts_give_tokens_per_host_cache_hit_and_agents_by_rate(monkeypatch, tmp_path):
    by_run = {
        "alpha-1": [_turn(58, "host-a", "build-1", 100, 0.1), _turn(59, "host-a", "build-1", 100, 0.1)],
        "beta-1": [_turn(59, "host-b", "build-2", 400, 0.4)],
    }
    _stub_turns(monkeypatch, by_run, [_turn(59, "host-a", "chair", 50, 0.05, source="session")])

    live = _edge(tmp_path, _targets())

    hosts = {h["host"]: h["series"][-2]["input"] for h in live["throughput"]["hosts"]}
    assert hosts == {"host-a": 150, "host-b": 400}
    assert live["throughput"]["system"][-2]["input"] == 550
    assert live["cache_hit_pct"][-2]["value"] == 75.0
    assert live["calls_per_min"][-2]["value"] == 3
    rates = [a["tokens_per_min"] for a in live["agents"]]
    assert rates == sorted(rates, reverse=True) and len(rates) == 3
    assert all(0.0 <= a["context_fill"] <= 1.0 for a in live["agents"])


def test_burn_rate_and_projection_flag_a_meter_over_its_ceiling(monkeypatch, tmp_path):
    _stub_turns(monkeypatch, {"alpha-1": [_turn(59, "host-a", "build-1", 100, 1.0)], "beta-1": []})

    high = _edge(tmp_path, _targets(), ceiling=0.93)["burn"]
    low = _edge(tmp_path, _targets(), ceiling=0.2)["burn"]

    assert high["usd_per_min_5m"] == 0.2 and high["usd_per_min_15m"] == 0.0667
    assert high["meters"]["five_hour"]["used_fraction"] == 0.4
    assert high["meters"]["five_hour"]["projected_fraction"] > high["meters"]["five_hour"]["used_fraction"]
    assert [m["over_ceiling"] for m in low["meters"].values()] == [True, True]
    assert low["meters"]["weekly"]["resets_at"] == "2026-11-08T04:00:00Z"


def test_landed_and_quarantined_end_times_fill_the_per_hour_series(monkeypatch, tmp_path):
    _stub_turns(monkeypatch, {"alpha-1": [], "beta-1": []})
    rows = {"runs": [
        {"run_id": "r1", "ended_at": "2026-11-06T11:20:00Z", "status": "landed"},
        {"run_id": "r2", "ended_at": "2026-11-06T10:20:00Z", "status": "quarantined"},
        {"run_id": "r3", "ended_at": "2026-11-06T10:40:00Z", "status": "quarantined"},
        {"run_id": "r4", "ended_at": "2026-11-06T10:50:00Z", "status": "stopped"},
    ]}

    live = _edge(tmp_path, _targets(), events=dash_feed._ended_events(rows))

    assert [p["value"] for p in live["lands_per_hour"]][-2:] == [1, 0]
    assert [p["value"] for p in live["quarantines_per_hour"]][-3:] == [2, 0, 0]


def test_an_unreadable_trace_directory_falls_back_to_the_zero_block(monkeypatch, tmp_path):
    def unreadable(*args, **kwargs):
        raise OSError("gone")

    _stub_turns(monkeypatch, {})
    monkeypatch.setattr(dash_feed.dash_live_turns_lane, "read_lane_turns", unreadable)

    assert _edge(tmp_path, _targets()) == dash_feed._zero_live(NOW)


def test_live_keys_equal_the_fixtures_key_structure(monkeypatch, tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_v1.json").read_text())["live"]
    _stub_turns(monkeypatch, {"alpha-1": [_turn(59, "host-a", "build-1", 100, 0.1)], "beta-1": []})

    live = _edge(tmp_path, _targets())

    assert _keys(live) == _keys(fixture)
    assert _keys(dash_feed._zero_live(NOW))["burn"] == _keys(fixture)["burn"]
    assert [len(live[k]) for k in ("calls_per_min", "cache_hit_pct", "lands_per_hour")] == [60, 60, 24]


_PRICES = {"claude-haiku-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}}
_USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 2000}


def _line(message_id, stamp, model="claude-haiku-4-5", **extra):
    """One assistant event as the CLI writes it; each turn costs 0.0285 at `_PRICES`."""
    message = {"id": message_id, "model": model, "usage": _USAGE}
    return json.dumps({"type": "assistant", "timestamp": stamp, "message": message, "session_id": "s", **extra})


def _write(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))


@pytest.fixture
def files(monkeypatch, tmp_path):
    """Two initiatives on two hosts: alpha on host-a (two turns), beta on host-b (one), a chair session and a subagent."""
    runs, projects = tmp_path / "runs", tmp_path / "projects"
    _write(runs / "alpha-1-trace" / "build-1.jsonl", _line("a1", "2026-11-06T11:59:10Z"), _line("a2", "2026-11-06T11:59:20Z"))
    _write(runs / "beta-1-trace" / "review-1.jsonl", _line("b1", "2026-11-06T11:59:30Z"))
    _write(projects / "p" / "chair123.jsonl", _line("c1", "2026-11-06T11:59:40Z", requestId="r1"))
    _write(projects / "p" / "chair123" / "subagents" / "agent-abc.jsonl", _line("s1", "2026-11-06T11:59:50Z", requestId="r2"))
    monkeypatch.setattr(dash_feed, "_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(dash_feed.chair_activity_read, "prices_for_profile", lambda profile: _PRICES)
    lanes = [_lane("alpha-1", "host-a"), console_screen.LaneRow("beta-1", "host-b", "", "p1", "review", 1, 3, 0.5, 0, 2)]
    return runs, dash_feed._lane_targets(lanes, {("beta", "p1", "t-b"): "building"}, [], {}, "host-a", NOW)


def test_real_trace_and_transcript_files_give_per_host_tokens_cache_hit_agents_and_burn(files):
    runs, targets = files

    live = _edge(runs, targets)

    series = {h["host"]: h["series"][-2] for h in live["throughput"]["hosts"]}
    assert series["host-a"] == {"at": "2026-11-06T11:59:00Z", "input": 4000, "output": 800, "cache_read": 200000}
    assert series["host-b"] == {"at": "2026-11-06T11:59:00Z", "input": 1000, "output": 200, "cache_read": 50000}
    assert live["calls_per_min"][-2]["value"] == 5
    assert live["cache_hit_pct"][-2]["value"] == 94.3  # 250000 / (5000 + 250000 + 10000)
    rates = [a["tokens_per_min"] for a in live["agents"]]
    assert len(rates) == 4 and rates == sorted(rates, reverse=True)
    assert live["agents"][0]["id"] == "build-1" and all(0 < a["context_fill"] <= 1 for a in live["agents"])
    burn = live["burn"]
    assert (burn["usd_per_min_5m"], burn["usd_per_min_15m"]) == (0.0285, 0.0095)  # five turns at 0.0285
    assert burn["meters"]["five_hour"]["projected_fraction"] == pytest.approx(0.7408, abs=1e-3)


def test_real_files_flag_the_projection_over_a_low_ceiling_only(files):
    runs, targets = files

    high = _edge(runs, targets, ceiling=0.93)["burn"]["meters"]["five_hour"]
    low = _edge(runs, targets, ceiling=0.5)["burn"]["meters"]["five_hour"]

    assert (high["over_ceiling"], low["over_ceiling"]) == (False, True)


def _wide_rows():
    """31 runs ended since Eastern midnight (05:00Z), newest first, then a landed run from before it, inside 24 hours."""
    newest = [{"run_id": f"n{i:02d}", "ended_at": f"2026-11-06T06:{59 - i:02d}:00Z", "status": "stopped"} for i in range(31)]
    old = {"run_id": "old", "ended_at": "2026-11-05T20:10:00Z", "status": "landed", "host": "host-b"}
    return {"runs": [*newest, old], "task_records": [], "node_calls": []}


def test_ended_events_reach_back_past_the_history_cut():
    rows = _wide_rows()

    assert "old" not in [r["run_id"] for r in dash_feed._history_rows(rows, NOW)["runs"]]  # the cut drops it
    assert dash_feed._ended_events(rows) == [{"ts": "2026-11-05T20:10:00Z", "outcome": "landed"}]
    assert dash_feed._run_hosts(rows) == {"old": "host-b"}


def test_a_catalog_context_window_fills_a_sonnet_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(dash_feed, "_PROJECTS_DIR", str(tmp_path / "no-projects"))
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        "models:\n  - id: claude-sonnet-5-5\n    aliases: [sonnet]\n    context_window: 200000\n"
        "    price: {input: 3.0, output: 15.0, cache_read: 0.3, cache_write: 3.75}\n",
        encoding="utf-8",
    )
    runs = tmp_path / "runs"
    _write(runs / "alpha-1-trace" / "build-1.jsonl", _line("a1", "2026-11-06T11:59:10Z", model="claude-sonnet-5-5"))
    targets = dash_feed._lane_targets([_lane("alpha-1", "host-a")], {}, [], {}, "host-a", NOW)

    live = _edge(runs, targets, profile={"provider_profile": str(tmp_path / "profile.yaml")})

    assert [a["context_fill"] for a in live["agents"]] == [0.265]  # (1000 + 50000 + 2000) / 200000
    assert live["burn"]["usd_per_min_5m"] == 0.0057


def test_gather_feed_carries_the_live_block_from_the_files_and_the_wide_store_rows(monkeypatch, tmp_path):
    runs, projects = tmp_path, tmp_path / "projects"
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        "models:\n  - id: claude-haiku-4-5\n    price: {input: 3.0, output: 15.0, cache_read: 0.3, cache_write: 3.75}\n",
        encoding="utf-8",
    )
    _write(runs / "alpha-1-trace" / "build-1.jsonl", _line("a1", "2026-11-06T11:59:10Z"), _line("a2", "2026-11-06T11:59:20Z"))
    _write(projects / "p" / "other.jsonl", _line("o1", "2026-11-06T11:59:40Z", requestId="r1"))
    sections = {
        "chair": [{"holder": "chair-loop", "state": "live", "minutes_ago": 1}],
        "spend": {
            "five_hour_fraction": 0.4, "five_hour_resets_at": "2026-11-06T14:00:00Z",
            "weekly_fraction": 0.5, "weekly_resets_at": "2026-11-08T04:00:00Z",
        },
        "hosts": [],
        "lanes": [console_screen.LaneRow("alpha-1", "host-b", "", "p1", "build", 1, 3, 0.5, 0, 2)],
    }
    split = {"five_hour": {"cox_seats_usd": 4.0}, "weekly": {"cox_seats_usd": 5.0}}
    monkeypatch.setattr(dash_feed, "_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(dash_feed, "_local_login", lambda local_name: None)
    monkeypatch.setattr(dash_feed.run_store, "initiative_short_ids", lambda runs_dir: {})
    monkeypatch.setattr(dash_feed.console_screen, "gather", lambda *args, **kwargs: sections)
    monkeypatch.setattr(dash_feed, "_spend", lambda *args: {})
    monkeypatch.setattr(dash_feed, "_local_identity", lambda runs_dir, profile: ("host-a", 3))
    monkeypatch.setattr(dash_feed, "_store_rows", lambda runs_dir, at, spend: _wide_rows())
    monkeypatch.setattr(
        dash_feed, "_activity",
        lambda *args: {"chair_agents": [], "spend_split": split, "spend_sources": {"chair_session": "none", "price_catalog": "ok"}},
    )

    feed = dash_feed.gather_feed(runs, runs, "2026-11-06T12:00:30Z", {"provider_profile": str(tmp_path / "profile.yaml")})

    live = feed["live"]
    assert {h["host"]: h["series"][-2]["input"] for h in live["throughput"]["hosts"]} == {"host-a": 1000, "host-b": 2000}
    assert live["lands_per_hour"][7] == {"at": "2026-11-05T20:00:00Z", "value": 1}  # before local midnight, outside the history cut
    assert (live["burn"]["usd_per_min_5m"], live["burn"]["usd_per_min_15m"]) == (0.0171, 0.0057)
    assert live["burn"]["meters"]["five_hour"]["used_fraction"] == 0.4
    assert [r["run"] for r in feed["runs"]] == ["alpha-1"] and feed["runs"][0]["machine"] == "host-b"
