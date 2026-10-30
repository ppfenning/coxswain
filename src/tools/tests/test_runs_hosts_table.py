"""`runs move --to` and `runs stop` resolve hosts from the hosts table the way `route launch --on` does."""

import json

import pytest

from agent_tools import chair_exec, cli, run_store, store_cli
from agent_tools.cli import main

RUN = "demo-1"
WS = "/srv/ws"


def _row(name, state="active", workspace_dir=WS):
    versions = {"workspace_dir": workspace_dir} if workspace_dir else {}
    return {"name": name, "ssh": f"{name}.example", "state": state, "versions_json": json.dumps(versions)}


@pytest.fixture
def table(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(run_store, "hosts", lambda runs_dir: list(rows))
    return rows


@pytest.fixture
def order(monkeypatch):
    """Fakes the pause, stop, clear_branches and relaunch edges; the list records which ran."""
    calls: list[str] = []

    def fake_pause(run_id, reason=None):
        calls.append("pause")
        return store_cli.Paused(reason=reason)

    def fake_perform(actions, deps, current_epoch, dry_run):
        calls.append(actions[0]["kind"])
        return [{"action": actions[0], "status": "done", "reason": ""}]

    monkeypatch.setattr(store_cli, "pause", fake_pause)
    monkeypatch.setattr(chair_exec, "perform", fake_perform)
    monkeypatch.setattr(cli, "_runs_stop_run_remote", lambda argv: calls.append("stop") or True)
    return calls


def _runs_dir(tmp_path, host="host-c"):
    runs = tmp_path / "ws" / "runs"
    runs.mkdir(parents=True)
    (runs / f"{RUN}.remote.json").write_text(json.dumps({"host": host, "launched_at": "2026-09-29T00:00:00Z"}))
    return runs


def _profile(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("team: t\n")
    return str(path)


def _move(tmp_path, runs, host):
    return main(["runs", "move", RUN, "--to", host, "--runs-dir", str(runs), "--profile", _profile(tmp_path)])


def _stop(tmp_path, runs, monkeypatch):
    monkeypatch.setattr(cli, "_runs_stop_is_ended", lambda runs_dir: lambda run_id: True)
    return main(["runs", "stop", RUN, "--runs-dir", str(runs), "--profile", _profile(tmp_path)])


def test_move_resolves_a_host_only_the_table_names(tmp_path, table, order, capsys):
    table.extend([_row("host-a"), _row("host-c")])
    assert _move(tmp_path, _runs_dir(tmp_path, "host-a"), "host-c") == 0
    assert capsys.readouterr().out == f"moved {RUN} -> host-c\n"
    assert order == ["pause", "stop", "clear_branches", "relaunch"]


def test_stop_resolves_the_run_host_from_the_table(tmp_path, table, order, monkeypatch, capsys):
    table.append(_row("host-c"))
    assert _stop(tmp_path, _runs_dir(tmp_path), monkeypatch) == 0
    assert capsys.readouterr().out == f"stopped {RUN}\n"
    assert order == ["stop"]


def test_move_to_an_unknown_name_is_refused_with_the_configured_names(tmp_path, table, order, capsys):
    table.extend([_row("host-a"), _row("host-b"), _row("host-c")])
    assert _move(tmp_path, _runs_dir(tmp_path), "ghost") == 1
    seen = capsys.readouterr()
    assert seen.out == f"refused {RUN}: no such host ghost\n"
    assert "configured: host-a, host-b, host-c" in seen.err
    assert order == []


def test_stop_of_a_run_on_an_unknown_host_lists_the_configured_names(tmp_path, table, order, monkeypatch, capsys):
    table.extend([_row("host-a"), _row("host-b")])
    assert _stop(tmp_path, _runs_dir(tmp_path, "host-c"), monkeypatch) == 1
    assert capsys.readouterr().out == f"refused {RUN}: host host-c not configured; configured: host-a, host-b\n"
    assert order == []


def test_move_to_a_row_with_no_workspace_dir_refuses_before_any_step(tmp_path, table, order, capsys):
    table.append(_row("host-c", workspace_dir=""))
    assert _move(tmp_path, _runs_dir(tmp_path, "host-a"), "host-c") == 1
    assert "host host-c has no workspace_dir" in capsys.readouterr().out
    assert order == []


def test_stop_on_a_row_with_no_workspace_dir_reports_no_stop(tmp_path, table, order, monkeypatch, capsys):
    table.append(_row("host-c", workspace_dir=""))
    assert _stop(tmp_path, _runs_dir(tmp_path), monkeypatch) == 1
    assert "host host-c has no workspace_dir" in capsys.readouterr().out
    assert order == []


def test_a_drained_row_is_refused_by_move_and_still_stoppable(tmp_path, table, order, monkeypatch, capsys):
    table.append(_row("host-c", state="drained"))
    assert _move(tmp_path, _runs_dir(tmp_path, "host-a"), "host-c") == 1
    assert capsys.readouterr().out == f"refused {RUN}: host host-c is not active\n"
    assert order == []
    assert _stop(tmp_path, _runs_dir(tmp_path.joinpath("again"), "host-c"), monkeypatch) == 0
    assert order == ["stop"]
