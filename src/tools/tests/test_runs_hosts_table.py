"""`runs move --to` and `runs stop` resolve host names from the hosts table, as `route launch --on` does."""

import json

from agent_tools import chair_exec, cli, run_store, runs_stop, store_cli
from agent_tools.cli import main

RUN = "demo-1"
NO_LANE_HOSTS = "team: t\n"


def _row(name, workspace_dir, state="active"):
    versions = {"workspace_dir": workspace_dir} if workspace_dir else {}
    return {"name": name, "ssh": f"{name}.example", "state": state, "versions_json": json.dumps(versions)}


def _setup(tmp_path, monkeypatch, rows, profile=NO_LANE_HOSTS):
    """A profile file and a fixed hosts table; returns the argv tail naming both, and the runs dir."""
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(profile)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(run_store, "hosts", lambda d: rows)
    return ["--profile", str(profile_path), "--runs-dir", str(runs_dir)], runs_dir


def _fake_edges(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(store_cli, "pause", lambda run_id, reason=None: order.append("pause") or store_cli.Paused(reason=reason))
    monkeypatch.setattr(runs_stop, "stop_run", lambda *a, **k: order.append("stop") or {"ok": True, "reason": ""})
    monkeypatch.setattr(
        chair_exec, "perform",
        lambda actions, deps, current_epoch, dry_run: order.append(actions[0]["kind"])
        or [{"action": actions[0], "status": "done", "reason": ""}],
    )
    return order


def test_move_resolves_a_host_that_only_the_table_has(tmp_path, monkeypatch, capsys):
    tail, _ = _setup(tmp_path, monkeypatch, [_row("omarchy", str(tmp_path / "ws"))])
    order = _fake_edges(monkeypatch)
    assert main(["runs", "move", RUN, "--to", "omarchy", *tail]) == 0
    assert capsys.readouterr().out == f"moved {RUN} -> omarchy\n"
    assert order == ["pause", "stop", "clear_branches", "relaunch"]


def test_move_to_an_unknown_name_is_refused_with_the_configured_names(tmp_path, monkeypatch, capsys):
    tail, _ = _setup(tmp_path, monkeypatch, [_row("omarchy", str(tmp_path / "ws"))])
    order = _fake_edges(monkeypatch)
    assert main(["runs", "move", RUN, "--to", "ghost", *tail]) == 1
    seen = capsys.readouterr()
    assert seen.out == f"refused {RUN}: no such host ghost\n"
    assert seen.err == "configured: omarchy\n"
    assert order == []


def test_move_to_an_active_row_with_no_workspace_dir_refuses_before_any_step(tmp_path, monkeypatch, capsys):
    tail, _ = _setup(tmp_path, monkeypatch, [_row("omarchy", "")])
    order = _fake_edges(monkeypatch)
    assert main(["runs", "move", RUN, "--to", "omarchy", *tail]) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"refused {RUN}: host omarchy has no workspace_dir")
    assert order == []


def test_move_to_a_drained_row_refuses_before_any_step(tmp_path, monkeypatch, capsys):
    tail, _ = _setup(tmp_path, monkeypatch, [_row("omarchy", str(tmp_path / "ws"), state="drained")])
    order = _fake_edges(monkeypatch)
    assert main(["runs", "move", RUN, "--to", "omarchy", *tail]) == 1
    assert capsys.readouterr().out == f"refused {RUN}: host omarchy is drained, not active\n"
    assert order == []


def test_move_with_an_unreadable_profile_and_no_table_refuses_with_the_error(tmp_path, monkeypatch, capsys):
    tail, _ = _setup(tmp_path, monkeypatch, [], profile="lane_hosts: [unclosed\n")
    order = _fake_edges(monkeypatch)
    assert main(["runs", "move", RUN, "--to", "omarchy", *tail]) == 1
    assert "not readable as YAML" in capsys.readouterr().out
    assert order == []


def _remote_run(runs_dir, host):
    (runs_dir / f"{RUN}.remote.json").write_text(json.dumps({"host": host, "launched_at": "2026-10-30T00:00:00Z"}))


def test_stop_of_a_remote_run_resolves_its_host_from_the_table(tmp_path, monkeypatch, capsys):
    workspace = str(tmp_path / "ws")
    tail, runs_dir = _setup(tmp_path, monkeypatch, [_row("omarchy", workspace)])
    _remote_run(runs_dir, "omarchy")
    sent: list[list[str]] = []
    monkeypatch.setattr(cli, "_runs_stop_run_remote", lambda argv: sent.append(argv) or True)
    monkeypatch.setattr(cli, "_runs_stop_is_ended", lambda d: lambda run: True)
    assert main(["runs", "stop", RUN, "--json", *tail]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "run": RUN, "host": "omarchy"}
    assert f"{workspace}/runs/{RUN}.pid" in sent[0][-1]


def test_stop_of_a_run_on_a_row_with_no_workspace_dir_reports_no_stop(tmp_path, monkeypatch, capsys):
    tail, runs_dir = _setup(tmp_path, monkeypatch, [_row("omarchy", "")])
    _remote_run(runs_dir, "omarchy")
    sent: list[list[str]] = []
    monkeypatch.setattr(cli, "_runs_stop_run_remote", lambda argv: sent.append(argv) or True)
    assert main(["runs", "stop", RUN, *tail]) == 1
    assert capsys.readouterr().out.startswith(f"refused {RUN}: host omarchy has no workspace_dir")
    assert sent == []


def test_stop_of_a_run_on_a_drained_host_still_resolves_it(tmp_path, monkeypatch, capsys):
    tail, runs_dir = _setup(tmp_path, monkeypatch, [_row("omarchy", str(tmp_path / "ws"), state="drained")])
    _remote_run(runs_dir, "omarchy")
    monkeypatch.setattr(cli, "_runs_stop_run_remote", lambda argv: True)
    monkeypatch.setattr(cli, "_runs_stop_is_ended", lambda d: lambda run: True)
    main(["runs", "stop", RUN, *tail])
    assert capsys.readouterr().out == f"stopped {RUN}\n"


def test_stop_of_a_run_on_an_unlisted_host_is_refused(tmp_path, monkeypatch, capsys):
    tail, runs_dir = _setup(tmp_path, monkeypatch, [_row("omarchy", str(tmp_path / "ws"))])
    _remote_run(runs_dir, "ghost")
    main(["runs", "stop", RUN, *tail])
    assert capsys.readouterr().out == f"refused {RUN}: host ghost not configured\n"


def test_stop_with_a_bad_profile_and_no_table_refuses_instead_of_raising(tmp_path, monkeypatch, capsys):
    tail, runs_dir = _setup(tmp_path, monkeypatch, [], profile="lane_hosts: [unclosed\n")
    _remote_run(runs_dir, "omarchy")
    main(["runs", "stop", RUN, *tail])
    assert "not readable as YAML" in capsys.readouterr().out


def test_setup_doctor_finds_a_host_only_the_table_has(tmp_path, monkeypatch, capsys):
    tail, _ = _setup(tmp_path, monkeypatch, [_row("omarchy", str(tmp_path / "ws"))], profile=f"team: t\nworkspace_dir: {tmp_path}\n")
    monkeypatch.setattr(cli, "_run_ssh", lambda argv: (0, '{"loggedIn": true}' if "claude auth status" in argv[-1] else "ok  git\n"))
    assert main(["setup", "doctor", "--profile", tail[1], "--host", "omarchy"]) == 0
    assert capsys.readouterr().out.startswith("doctor on omarchy (omarchy.example)\n")
