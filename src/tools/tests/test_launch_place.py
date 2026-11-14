import argparse
import datetime
import json
import socket
import sqlite3
import subprocess

import pytest
from test_cli_launch_on import _setup
from test_route_cli import _unmeasured_window

from agent_tools import cli, launch_place, usage_window
from agent_tools.chair_facts import dispatch_facts
from agent_tools.chair_plan_fill import best_host, host_free_slots
from agent_tools.cli import main
from agent_tools.launch_place import Placement, Refusal, format_refusal, place, place_local
from agent_tools.remote_argv import launch_argv

pytestmark = pytest.mark.usefixtures("board_store")  # the store is the board; these launches read its rows

NOW = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=datetime.UTC)
FRESH = "2026-10-30T11:59:00+00:00"
WORKSPACE = json.dumps({"workspace_dir": "/srv/ws"})


def _row(name, capacity, weight=1, state="active", beat=FRESH, versions=WORKSPACE):
    return {"name": name, "ssh": name, "capacity": capacity, "weight": weight, "state": state, "beat_at": beat, "versions_json": versions}


@pytest.fixture(autouse=True)
def _no_ccusage(monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)


def test_place_picks_the_host_the_chair_tick_picks_for_the_same_table():
    rows = [_row("here", 2), _row("a", 4, weight=1), _row("b", 4, weight=3), _row("stale", 9, beat="2026-10-30T10:00:00+00:00")]
    live = {"here": 2, "a": 1}
    facts = {"dispatch": dispatch_facts({"max_in_flight": 1}, ["here", "a", "b"], live, {"here": 2, "a": 4, "b": 4}, {"here": 1, "a": 1, "b": 3}), "login_hosts": rows}
    chair_pick = best_host(frozenset(), host_free_slots(facts))[0]  # type: ignore[arg-type]
    assert place(rows, live, NOW, local="here") == Placement(chair_pick, live.get(chair_pick, 0), 4)
    assert chair_pick == "b"


def test_place_skips_a_login_lapsed_host():
    rows = [_row("a", 4, versions=json.dumps({"workspace_dir": "/srv/ws", "login_ok": False})), _row("b", 1)]
    assert place(rows, {}, NOW).host == "b"


def test_full_local_host_refuses_and_force_launches():
    rows = [_row("here", 2)]
    assert place_local("here", rows, {"here": 2}, force=False) == Refusal((("here", 2, 2, ""),))
    assert place_local("here", rows, {"here": 2}, force=True) == Placement("here", 2, 2)


def test_every_host_full_refuses_with_each_in_use_and_cap():
    rows = [_row("here", 2), _row("a", 1), _row("off", 5, state="draining")]
    refusal = place(rows, {"here": 2, "a": 1}, NOW, local="here")
    assert isinstance(refusal, Refusal)
    assert format_refusal(refusal) == ["here 2/2", "a 1/1", "off 0/5  (draining)"]


def test_local_excluded_places_on_the_lane_host_though_local_has_free_lanes():
    rows = [_row("here", 2, weight=3), _row("a", 2)]
    assert place(rows, {}, NOW, local="here", local_excluded=True) == Placement("a", 0, 2)


def test_local_excluded_with_the_lane_host_full_refuses_naming_the_local_reason():
    rows = [_row("here", 2, weight=3), _row("a", 2)]
    refusal = place(rows, {"a": 2}, NOW, local="here", local_excluded=True)
    assert refusal == Refusal((("here", 0, 2, "decompose only"), ("a", 2, 2, "")))
    assert format_refusal(refusal) == ["here 0/2  (decompose only)", "a 2/2"]


def test_local_not_excluded_is_still_chosen():
    rows = [_row("here", 2, weight=3), _row("a", 2)]
    assert place(rows, {}, NOW, local="here") == Placement("here", 0, 2)
    assert place(rows, {}, NOW, local="here", local_excluded=False) == Placement("here", 0, 2)


def test_idle_remote_host_beaten_by_the_scheduled_login_check_stays_placeable():
    beat = (NOW - datetime.timedelta(minutes=25)).isoformat()
    versions = json.dumps({"workspace_dir": "/srv/ws", "login_ok": True, "login_checked_at": beat})
    rows = [_row("here", 1), _row("idle", 4, beat=beat, versions=versions)]
    assert place(rows, {"here": 1}, NOW, local="here") == Placement("idle", 0, 4)


def test_remote_host_two_login_check_intervals_overdue_is_refused_as_stale():
    beat = (NOW - datetime.timedelta(minutes=61)).isoformat()
    rows = [_row("here", 1), _row("idle", 4, beat=beat)]
    refusal = place(rows, {"here": 1}, NOW, local="here")
    assert isinstance(refusal, Refusal)
    assert format_refusal(refusal)[-1] == "idle 0/4  (stale beat)"


def _three_hosts():
    return [_row("heavy", 4, weight=4), _row("mid-a", 3, weight=3), _row("mid-b", 3, weight=3)]


def test_a_host_carrying_live_lanes_loses_to_idle_hosts_of_near_weight():
    assert place(_three_hosts(), {"heavy": 3}, NOW).host == "mid-a"


def test_three_successive_places_on_idle_hosts_land_on_three_hosts():
    rows = _three_hosts()
    first = place(rows, {}, NOW).host
    second = place(rows, {first: 1}, NOW).host
    third = place(rows, {first: 1, second: 1}, NOW).host
    assert [first, second, third] == ["heavy", "mid-a", "mid-b"]


def _store(ws, local, capacity):
    conn = sqlite3.connect(ws / "runs" / "cox.db")
    conn.execute("CREATE TABLE hosts (name TEXT, ssh TEXT, capacity INTEGER, state TEXT, beat_at TEXT, versions_json TEXT)")
    conn.execute("INSERT INTO hosts VALUES (?, ?, ?, 'active', ?, ?)", (local, local, capacity, FRESH, WORKSPACE))
    conn.commit()
    conn.close()


def test_route_launch_dry_run_prints_the_chosen_host_and_starts_nothing(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    _store(ws, socket.gethostname(), 5)

    def refuse(*args, **kwargs):
        assert not kwargs.get("start_new_session"), "a lane was started"
        return real(*args, **kwargs)

    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", refuse)
    code = main([*argv, "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert f"placed on {socket.gethostname()} (0/5)" in out
    assert "dry-run:" in out


def test_route_launch_without_room_exits_nonzero_listing_each_host(tmp_path, capsys):
    ws, _, argv = _setup(tmp_path)
    _store(ws, "other", 0)
    code = main([*argv, "--dry-run"])
    assert code == 2
    assert "other 0/0" in capsys.readouterr().out


def test_lane_host_or_refuse_forced_local_refuses_when_full_unless_force(tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    _store(ws, socket.gethostname(), 0)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\n", encoding="utf-8")
    args = argparse.Namespace(profile=str(profile), on="local", local=False, force=False, graph="epic", tier_ceiling=None, effort_ceiling=None, fix_attempts=None)
    assert cli._lane_host_or_refuse(args, {"workspace_dir": str(ws)}) == (None, 2)
    assert f"{socket.gethostname()} 0/0" in capsys.readouterr().out
    args.force = True
    assert cli._lane_host_or_refuse(args, {"workspace_dir": str(ws)}) == (None, None)


def test_the_remote_launch_argv_carries_local():
    assert launch_argv("init-dir", "run-1", "lbl")[:5] == ["cox", "route", "launch", "epic", "--local"]


def test_the_remote_route_launch_never_places_again(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    _store(ws, socket.gethostname(), 5)
    remote = launch_argv(argv[argv.index("--initiative") + 1], "run-1", "lbl")[1:]

    def placed_again(*args, **kwargs):
        raise AssertionError("the remote launch was placed a second time")

    monkeypatch.setattr(launch_place, "place", placed_again)
    code = main([*remote, "--profile", argv[argv.index("--profile") + 1], "--repo", argv[argv.index("--repo") + 1], "--dry-run"])
    assert code == 0
    assert f"placed on {socket.gethostname()} (0/5)" in capsys.readouterr().out
