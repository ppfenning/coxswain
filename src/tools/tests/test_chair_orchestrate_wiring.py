from __future__ import annotations

import argparse
import dataclasses
import json
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair_orchestrate, chair_run, cli, launch_place

HOSTS_DDL = ("CREATE TABLE hosts (name TEXT PRIMARY KEY, ssh TEXT, capacity INTEGER, state TEXT, beat_at TEXT,"
             " versions_json TEXT, updated_at TEXT, updated_by TEXT)")
BEAT = '{"login_ok": true, "workspace_dir": "/srv/ws"}'
NOTICE = f"chair run: {chair_orchestrate.FALLBACK_NOTICE}"
ROW = {"max_in_flight": 3}
LOCAL = socket.gethostname()
STALE = "2020-01-01T00:00:00Z"


def _fresh() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_hosts_table(runs: Path) -> None:
    runs.mkdir()
    sqlite3.connect(runs / "cox.db").execute(HOSTS_DDL).connection.close()


def _add_host(runs: Path, name: str, beat_at: str, state: str = "active") -> None:
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute(
        "INSERT INTO hosts VALUES (?, ?, 4, ?, ?, ?, ?, 'chair')", (name, name, state, beat_at, BEAT, beat_at),
    )
    conn.commit()
    conn.close()


def _chair(tmp_path: Path, local_lanes: str, profile_text: str = ""):
    """The deps of a dry-run chair on host `h`, a cartridge setting `local_lanes`, an empty hosts table, and its output lines."""
    runs = tmp_path / "runs"
    _empty_hosts_table(runs)
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "cartridge.yaml").write_text(
        f"policy:\n  dispatch:\n    local_lanes: {local_lanes}\n", encoding="utf-8",
    )
    (tmp_path / "profile.yaml").write_text(profile_text, encoding="utf-8")
    profile = {"cartridges_dir": str(tmp_path), "team": "team"}
    lines: list[str] = []
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", True, lines.append, tmp_path / "profile.yaml", "files")
    return runs, deps, lines


def test_decompose_with_no_lane_host_builds_locally_and_says_so_once(tmp_path) -> None:
    _, deps, lines = _chair(tmp_path, "decompose")

    facts = deps.facts_deps.dispatch(ROW)  # type: ignore[misc]
    deps.facts_deps.dispatch(ROW)  # type: ignore[misc]

    assert "local_lanes" not in facts
    assert lines.count(NOTICE) == 1


def test_the_loop_prints_one_notice_per_tick(tmp_path) -> None:
    """Drives `chair_run.tick`, so the once-per-tick claim rests on the loop's own beat, not a hand-made one."""
    _, deps, lines = _chair(tmp_path, "decompose")

    def gather(facts_deps, now):
        facts_deps.dispatch(ROW)
        facts_deps.dispatch(ROW)
        return {"lease": {"released": False, "stale": False}}

    looped = dataclasses.replace(deps, gather=gather, plan=lambda facts, now: [], perform=lambda *args: [], status_reader=None)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    chair_run.tick(looped, True, now)
    chair_run.tick(looped, True, now)

    assert lines.count(NOTICE) == 2


def test_an_active_host_restores_decompose_and_drops_the_notice(tmp_path) -> None:
    runs, deps, lines = _chair(tmp_path, "decompose")
    deps.facts_deps.dispatch(ROW)  # type: ignore[misc]
    _add_host(runs, "host-a", _fresh())
    deps.beat()
    before = len(lines)

    facts = deps.facts_deps.dispatch(ROW)  # type: ignore[misc]

    assert facts["local_lanes"] == "decompose"
    assert lines[before:] == []


def test_a_host_with_a_stale_beat_is_not_a_lane_host_for_the_mode(tmp_path) -> None:
    runs, deps, lines = _chair(tmp_path, "decompose")
    _add_host(runs, "host-a", STALE)

    facts = deps.facts_deps.dispatch(ROW)  # type: ignore[misc]

    assert "local_lanes" not in facts
    assert lines.count(NOTICE) == 1


def test_a_host_that_never_beat_is_not_a_lane_host_for_the_mode(tmp_path) -> None:
    """Placement counts a blank beat as stale, so a just-added host must not keep the chair in `decompose`."""
    runs, deps, lines = _chair(tmp_path, "decompose")
    _add_host(runs, "host-a", "")

    facts = deps.facts_deps.dispatch(ROW)  # type: ignore[misc]

    assert "local_lanes" not in facts
    assert lines.count(NOTICE) == 1


def test_a_host_whose_login_lapsed_is_not_a_lane_host_for_the_mode(tmp_path) -> None:
    runs, deps, lines = _chair(tmp_path, "decompose")
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute(
        "INSERT INTO hosts VALUES ('host-a', 'host-a', 4, 'active', ?, ?, ?, 'chair')",
        (_fresh(), '{"login_ok": false, "workspace_dir": "/srv/ws"}', _fresh()),
    )
    conn.commit()
    conn.close()

    facts = deps.facts_deps.dispatch(ROW)  # type: ignore[misc]

    assert "local_lanes" not in facts
    assert lines.count(NOTICE) == 1


@pytest.mark.parametrize("beat_at", [STALE, "", None, "fresh"])
@pytest.mark.parametrize("login_ok", [True, False, None])
def test_the_loop_has_a_usable_host_exactly_when_placement_can_use_one(beat_at, login_ok) -> None:
    """Runs the real `launch_place.place` with this machine excluded, so the two sets cannot drift apart unseen."""
    rows = [
        {"name": LOCAL, "ssh": LOCAL, "capacity": 2, "weight": 1, "state": "active", "beat_at": _fresh(), "versions_json": "{}"},
        {"name": "far", "ssh": "me@far", "capacity": 1, "weight": 1, "state": "active",
         "beat_at": _fresh() if beat_at == "fresh" else beat_at,
         "versions_json": json.dumps({"workspace_dir": "/srv/ws", "login_ok": login_ok})},
    ]
    now = datetime.now(UTC)
    usable = cli._orchestration_hosts(cli._lane_host_candidates("", rows, LOCAL), rows, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    placed = launch_place.place(rows, {}, now, LOCAL, 2, local_excluded=True)

    assert bool(usable) == isinstance(placed, launch_place.Placement)


def test_an_unreadable_lane_hosts_block_says_that_not_that_no_host_is_active(tmp_path) -> None:
    _, deps, lines = _chair(tmp_path, "decompose", "lane_hosts:\n  - 5\n")

    facts = deps.facts_deps.dispatch(ROW)  # type: ignore[misc]

    assert "local_lanes" not in facts
    assert NOTICE not in lines
    assert any(line.startswith("chair run: lane_hosts unreadable (") and line.endswith("building on the chair machine") for line in lines)


def test_any_configured_prints_nothing_and_carries_no_local_lanes(tmp_path) -> None:
    _, deps, lines = _chair(tmp_path, "any")

    facts = deps.facts_deps.dispatch(ROW)  # type: ignore[misc]

    assert "local_lanes" not in facts
    assert NOTICE not in lines


def _place_epic(tmp_path, monkeypatch, remote_beat: str) -> tuple[object, int | None]:
    """`_place_or_refuse` for an epic under `local_lanes: decompose` with this machine and one remote lane host in the table."""
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "cartridge.yaml").write_text("policy:\n  dispatch:\n    local_lanes: decompose\n", encoding="utf-8")
    rows = [
        {"name": LOCAL, "ssh": LOCAL, "capacity": 2, "weight": 1, "state": "active", "beat_at": _fresh(), "versions_json": "{}"},
        {"name": "far", "ssh": "me@far", "capacity": 1, "weight": 1, "state": "active", "beat_at": remote_beat,
         "versions_json": json.dumps({"workspace_dir": "/srv/ws"})},
    ]
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: rows)
    monkeypatch.setattr(cli.run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(cli, "_local_max_in_flight", lambda runs_dir, profile, local: 2)
    monkeypatch.setattr(cli, "_read_text_or_none", lambda path: "")
    monkeypatch.setattr(cli, "_profile_path", lambda a: "profile.yaml")
    monkeypatch.setattr(cli, "_dropped_ceilings", lambda a: ())
    monkeypatch.setattr(cli, "_epic_repo", lambda a, profile: None)
    monkeypatch.setattr(cli, "_toolchain_refusal", lambda *a, **k: None)
    profile = {"workspace_dir": "/ws", "cartridges_dir": str(tmp_path), "team": "team"}
    return cli._place_or_refuse(argparse.Namespace(graph="epic", force=False), profile, False)


@pytest.mark.parametrize("beat_at", [STALE, ""])
def test_an_epic_is_placed_on_this_machine_when_no_lane_host_is_usable(beat_at, tmp_path, monkeypatch, capsys) -> None:
    host, code = _place_epic(tmp_path, monkeypatch, beat_at)

    assert (host, code) == (None, None)
    assert f"placed on {LOCAL} (0/2)" in capsys.readouterr().out


def test_an_epic_still_goes_to_the_lane_host_when_one_is_usable(tmp_path, monkeypatch, capsys) -> None:
    host, code = _place_epic(tmp_path, monkeypatch, _fresh())

    assert code is None and host is not None and host.name == "far"  # type: ignore[attr-defined]
    assert "placed on far (0/1)" in capsys.readouterr().out
