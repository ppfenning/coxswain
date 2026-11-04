import argparse
import datetime
import json
import re
import socket
import sqlite3

import pytest
from test_route_cli import _init_repo, _unmeasured_window, _write_harness

from agent_tools import cli, usage_window
from agent_tools.cli import main
from agent_tools.remote_argv import decompose_launch_argv, rsync_file_argv, ssh_argv

LOCAL = socket.gethostname()
REPO = "~/proj"
INTAKE = "work/intake/idea1.md"
FAR_WORKSPACE = "/srv/far-ws"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)
    monkeypatch.setattr(cli, "_host_auth_output", lambda argv: '{"loggedIn": true}')
    monkeypatch.setenv("HOME", str(tmp_path))


def _setup(tmp_path, local_capacity):
    """A workspace holding one intake whose repo: is `~/proj`, and a hosts table of this machine and a free `far`."""
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    _init_repo(tmp_path / "proj")
    idea = ws / INTAKE
    idea.parent.mkdir(parents=True)
    idea.write_text(f"---\ntitle: Idea\nrepo: {REPO}\n---\n\nBody\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nharness_dir: {harness_dir}\nworkspace_dir: {ws}\n"
        "cartridges_dir: /opt/cartridges\nprovider_profile: /opt/providers/acme.yaml\n",
        encoding="utf-8",
    )
    beat = datetime.datetime.now(datetime.UTC).isoformat()
    conn = sqlite3.connect(ws / "runs" / "cox.db")
    conn.execute("CREATE TABLE hosts (name TEXT, ssh TEXT, capacity INTEGER, state TEXT, beat_at TEXT, versions_json TEXT)")
    conn.execute("INSERT INTO hosts VALUES (?, ?, ?, 'active', ?, ?)", (LOCAL, LOCAL, local_capacity, beat, "{}"))
    conn.execute("INSERT INTO hosts VALUES ('far', 'me@far', 5, 'active', ?, ?)", (beat, json.dumps({"workspace_dir": FAR_WORKSPACE})))
    conn.commit()
    conn.close()
    argv = ["route", "launch", "decompose", "--profile", str(profile), "--idea", str(idea), "--initiative-id", "demo2", "--label", "lbl"]
    return ws, argv


def _fake_edge(monkeypatch, calls):
    def edge(cwd):
        def run(argv):
            calls.append(argv)
            return 0

        return run, None

    monkeypatch.setattr(cli, "_remote_edge", edge)


def _probe_log(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_toolchain_refusal", lambda name, ssh, tools, env=None, *, repo=None: seen.append((name, repo)))
    return seen


def _assert_remote_launch(calls, out, ws):
    run_id = re.search(r"^run (\S+)$", out, re.MULTILINE).group(1)
    assert calls[0] == rsync_file_argv(INTAKE, f"me@far:{FAR_WORKSPACE}")
    assert calls[-1] == ssh_argv("me@far", decompose_launch_argv(f"{FAR_WORKSPACE}/{INTAKE}", "demo2", run_id, "lbl"))
    record = json.loads((ws / "runs" / f"{run_id}.remote.json").read_text(encoding="utf-8"))
    assert record["host"] == "far" and record["graph"] == "decompose" and record["initiative"] == "demo2"
    assert record["repo"] == REPO


def test_a_decompose_on_a_remote_host_builds_the_remote_argv_from_the_intake(tmp_path, monkeypatch, capsys):
    ws, argv = _setup(tmp_path, local_capacity=0)
    calls = []
    _fake_edge(monkeypatch, calls)
    code = main(argv)
    out = capsys.readouterr().out
    assert code == 0 and "host far" in out
    _assert_remote_launch(calls, out, ws)
    assert any("proj" in call[-1] and str(tmp_path) not in call[-1] for call in calls[1:-1]), "the repo sync names the home-relative repo"


def test_a_full_local_host_places_a_decompose_remotely_and_probes_the_intake_repo(tmp_path, monkeypatch, capsys):
    ws, argv = _setup(tmp_path, local_capacity=0)
    _fake_edge(monkeypatch, [])
    probed = _probe_log(monkeypatch)
    code = main(argv)
    out = capsys.readouterr().out
    assert code == 0 and "placed on far (0/5)" in out
    assert ("far", REPO) in probed


def test_local_keeps_a_decompose_on_this_machine(tmp_path, monkeypatch, capsys):
    ws, argv = _setup(tmp_path, local_capacity=2)
    calls = []
    _fake_edge(monkeypatch, calls)
    assert main([*argv, "--local", "--dry-run"]) == 0
    assert f"placed on {LOCAL} (0/2)" in capsys.readouterr().out
    assert calls == []


def test_local_on_a_full_host_is_refused_without_force_even_with_a_free_remote(tmp_path, monkeypatch, capsys):
    ws, argv = _setup(tmp_path, local_capacity=0)
    calls = []
    _fake_edge(monkeypatch, calls)
    assert main([*argv, "--local", "--dry-run"]) == 2
    assert f"{LOCAL} 0/0" in capsys.readouterr().out
    assert calls == []


def test_a_decompose_placed_remotely_still_refuses_a_dropped_ceiling(tmp_path, monkeypatch, capsys):
    ws, argv = _setup(tmp_path, local_capacity=0)
    calls = []
    _fake_edge(monkeypatch, calls)
    assert main([*argv, "--tier-ceiling", "deep"]) == 2
    assert "placed on far, which does not carry --tier-ceiling" in capsys.readouterr().out
    assert calls == []


def test_the_intake_repo_is_read_as_recorded_and_is_none_without_one(tmp_path):
    with_repo = tmp_path / "a.md"
    with_repo.write_text(f"---\nrepo: {REPO}\n---\n\nBody\n", encoding="utf-8")
    without = tmp_path / "b.md"
    without.write_text("---\ntitle: T\n---\n\nBody\n", encoding="utf-8")
    assert cli._intake_repo(argparse.Namespace(idea=str(with_repo))) == REPO
    assert cli._intake_repo(argparse.Namespace(idea=str(without))) is None
    assert cli._intake_repo(argparse.Namespace(idea=str(tmp_path / "missing.md"))) is None
