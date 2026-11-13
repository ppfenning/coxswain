from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_tools import chair_orchestrate, cli

HOSTS_DDL = ("CREATE TABLE hosts (name TEXT PRIMARY KEY, ssh TEXT, capacity INTEGER, state TEXT, beat_at TEXT,"
             " versions_json TEXT, updated_at TEXT, updated_by TEXT)")
BEAT = '{"login_ok": true, "workspace_dir": "/srv/ws"}'
NOTICE = f"chair run: {chair_orchestrate.FALLBACK_NOTICE}"
DECOMPOSE = "policy:\n  dispatch:\n    local_lanes: decompose\n"


def _hosts_table(runs: Path, *rows) -> None:
    runs.mkdir()
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute(HOSTS_DDL)
    conn.commit()
    conn.close()
    _add_hosts(runs, *rows)


def _add_hosts(runs: Path, *rows) -> None:
    conn = sqlite3.connect(runs / "cox.db")
    conn.executemany(
        "INSERT INTO hosts VALUES (?, ?, ?, ?, '2026-09-26T11:58:00Z', ?, '2026-09-26T11:58:00Z', 'chair')", rows,
    )
    conn.commit()
    conn.close()


def _deps(tmp_path: Path, monkeypatch, cartridge: str, *rows):
    runs = tmp_path / "runs"
    _hosts_table(runs, *rows)
    (tmp_path / "example-team").mkdir()
    (tmp_path / "example-team" / "cartridge.yaml").write_text(cartridge, encoding="utf-8")
    profile = {"cartridges_dir": str(tmp_path), "team": "example-team"}
    monkeypatch.setattr(cli.store_cli, "runner", lambda runs_dir: (lambda argv: (0, "{}")))  # local-host registration writes nothing
    said: list[str] = []
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", True, said.append, tmp_path / "profile.yaml", "files")
    return runs, deps, said


def _tick(deps) -> dict:
    deps.beat()
    return deps.facts_deps.dispatch({"max_in_flight": 3})  # type: ignore[misc]


def test_decompose_with_no_lane_host_builds_locally_and_says_so(tmp_path, monkeypatch) -> None:
    _, deps, said = _deps(tmp_path, monkeypatch, DECOMPOSE)
    facts = _tick(deps)
    assert "local_lanes" not in facts
    assert said.count(NOTICE) == 1


def test_two_ticks_give_one_notice_each_and_a_second_dispatch_in_a_tick_adds_none(tmp_path, monkeypatch) -> None:
    _, deps, said = _deps(tmp_path, monkeypatch, DECOMPOSE)
    _tick(deps)
    deps.facts_deps.dispatch({"max_in_flight": 3})  # type: ignore[misc]
    assert said.count(NOTICE) == 1
    _tick(deps)
    assert said.count(NOTICE) == 2


def test_an_active_host_restores_decompose_and_drops_the_notice(tmp_path, monkeypatch) -> None:
    runs, deps, said = _deps(tmp_path, monkeypatch, DECOMPOSE)
    assert "local_lanes" not in _tick(deps)
    assert said.count(NOTICE) == 1
    _add_hosts(runs, ("host-a", "host-a", 8, "active", BEAT))
    assert _tick(deps)["local_lanes"] == "decompose"  # type: ignore[typeddict-item]
    assert said.count(NOTICE) == 1


def test_an_active_host_from_the_start_is_silent_and_decompose(tmp_path, monkeypatch) -> None:
    _, deps, said = _deps(tmp_path, monkeypatch, DECOMPOSE, ("host-a", "host-a", 8, "active", BEAT))
    assert _tick(deps)["local_lanes"] == "decompose"  # type: ignore[typeddict-item]
    assert NOTICE not in said


def test_the_default_any_mode_is_silent_with_no_lane_host(tmp_path, monkeypatch) -> None:
    _, deps, said = _deps(tmp_path, monkeypatch, "policy:\n  dispatch:\n    max_in_flight: 8\n")
    assert "local_lanes" not in _tick(deps)
    assert NOTICE not in said
