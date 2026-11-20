import json

from agent_tools import epic, run_store, store_cli
from agent_tools.cli import main

RUN = "demo-1"


def _pidfile(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / f"{RUN}.pid").write_text("4242", encoding="utf-8")
    return runs_dir


def test_runs_stop_prints_stopped_and_exits_0(tmp_path, monkeypatch, capsys):
    runs_dir = _pidfile(tmp_path)
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None, now=None: False)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    code = main(["runs", "stop", RUN, "--runs-dir", str(runs_dir)])
    out = capsys.readouterr().out
    assert out == f"stopped {RUN}\n"
    assert code == 0


def test_runs_stop_resolves_a_short_id_to_the_run_holding_the_pidfile(tmp_path, monkeypatch, capsys):
    runs_dir = _pidfile(tmp_path)
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None, now=None: False)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    monkeypatch.setattr(run_store, "resolve_run_id", lambda rd, token: RUN if token == "I412-7" else None)
    code = main(["runs", "stop", "I412-7", "--runs-dir", str(runs_dir)])
    out = capsys.readouterr().out
    assert out == f"stopped {RUN}\n"
    assert code == 0


def test_runs_stop_json_prints_ok_true(tmp_path, monkeypatch, capsys):
    runs_dir = _pidfile(tmp_path)
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None, now=None: False)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    code = main(["runs", "stop", RUN, "--runs-dir", str(runs_dir), "--json"])
    out = capsys.readouterr().out
    assert json.loads(out)["ok"] is True
    assert code == 0


def test_runs_pause_prints_paused_and_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(store_cli, "pause", lambda store_url, run_id, reason=None: store_cli.Paused(reason=None))
    code = main(["runs", "pause", RUN])
    out = capsys.readouterr().out
    assert out == f"paused {RUN}\n"
    assert code == 0


def test_runs_pause_json(monkeypatch, capsys):
    monkeypatch.setattr(store_cli, "pause", lambda store_url, run_id, reason=None: store_cli.Paused(reason=None))
    code = main(["runs", "pause", RUN, "--json"])
    out = capsys.readouterr().out
    assert json.loads(out)["outcome"] == "paused"
    assert code == 0


def test_runs_resume_prints_resumed_and_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(store_cli, "resume", lambda store_url, run_id: store_cli.Resumed())
    code = main(["runs", "resume", RUN])
    out = capsys.readouterr().out
    assert out == f"resumed {RUN}\n"
    assert code == 0


def test_runs_resume_json(monkeypatch, capsys):
    monkeypatch.setattr(store_cli, "resume", lambda store_url, run_id: store_cli.Resumed())
    code = main(["runs", "resume", RUN, "--json"])
    out = capsys.readouterr().out
    assert json.loads(out)["outcome"] == "resumed"
    assert code == 0


def test_runs_pause_failed_prints_to_stderr_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(
        store_cli, "pause", lambda store_url, run_id, reason=None: store_cli.Failed(code=2, detail="store unreachable")
    )
    code = main(["runs", "pause", RUN])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"failed {RUN}: store unreachable (exit 2)\n"
    assert code == 1


def test_runs_pause_not_available_prints_to_stderr_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(store_cli, "pause", lambda store_url, run_id, reason=None: store_cli.NotAvailable("cannot open the store"))
    code = main(["runs", "pause", RUN])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"failed {RUN}: the store is not available: cannot open the store\n"
    assert code == 1


def test_runs_pause_resolves_a_short_id_and_writes_to_the_same_store_url_it_resolved_with(tmp_path, monkeypatch, capsys):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    resolved_in = []

    def fake_resolve(rd, token):
        resolved_in.append((rd, token))
        return "canonical-1"

    monkeypatch.setattr(run_store, "resolve_run_id", fake_resolve)
    calls = []

    def fake_pause(store_url, run_id, reason=None):
        calls.append((store_url, run_id))
        return store_cli.Paused(reason=None)

    monkeypatch.setattr(store_cli, "pause", fake_pause)
    code = main(["runs", "pause", "I412-7", "--runs-dir", str(runs_dir)])
    out = capsys.readouterr().out
    assert out == "paused canonical-1\n"
    assert code == 0
    assert resolved_in == [(str(runs_dir), "I412-7")] or resolved_in == [(runs_dir, "I412-7")]
    assert calls == [(run_store.store_url_for(runs_dir), "canonical-1")]
