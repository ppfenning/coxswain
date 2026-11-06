from __future__ import annotations

import socket
import sys
import types

import pytest

from agent_tools import cli
from agent_tools.events import Event

ROWS = [{"name": "host-a"}, {"name": "host-b"}]


def _stub(monkeypatch, tmp_path, code=0):
    calls: list[tuple] = []

    def deploy_release(*args):
        calls.append(args)
        return code

    monkeypatch.setitem(sys.modules, "agent_tools.release_deploy_edge", types.SimpleNamespace(deploy_release=deploy_release))
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda a: (None, tmp_path, None))
    monkeypatch.setattr(cli, "_load_lazy_modules", lambda: None)
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: ROWS)
    return calls


def test_deploy_calls_the_edge_once_with_the_version_the_rows_and_this_host(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, tmp_path)
    assert cli.main(["release", "deploy", "2026.11.6"]) == 0
    assert len(calls) == 1
    version, rows, local, run, notify = calls[0]
    assert (version, rows, local) == ("2026.11.6", ROWS, socket.gethostname())
    assert callable(run) and notify is cli._release_notify


@pytest.mark.parametrize("code", [0, 1, 7])
def test_deploy_returns_the_edges_exit_code(monkeypatch, tmp_path, code):
    _stub(monkeypatch, tmp_path, code)
    assert cli.main(["release", "deploy", "2026.11.6"]) == code


def test_deploy_runner_is_the_subprocess_wrapper_and_joins_stdout_and_stderr(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, tmp_path)
    cli.main(["release", "deploy", "2026.11.6"])
    run = calls[0][3]
    assert run(["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]) == (3, "out\nerr\n")
    assert run(["/no/such/program"])[0] == 127


@pytest.mark.parametrize("argv", [["release", "0.15.0", "--dry-run"], ["release", "check", "--json"], ["release"]])
def test_other_release_subcommands_print_the_moved_pointer_and_exit_two(monkeypatch, tmp_path, capsys, argv):
    calls = _stub(monkeypatch, tmp_path)
    assert cli.main(argv) == 2
    assert capsys.readouterr().out == cli._DEV_MOVED + "\n"
    assert calls == []


def test_deploy_without_a_version_is_an_argparse_refusal(monkeypatch, tmp_path, capsys):
    calls = _stub(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as refused:
        cli.main(["release", "deploy"])
    assert refused.value.code == 2
    assert "VERSION" in capsys.readouterr().err
    assert calls == []


def test_help_lists_release_with_its_example():
    text = cli.build_parser().format_help()
    assert "cox release deploy 2026.11.6" in text
    assert cli._RELEASE_HELP.split(" (")[0] in text


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (Event("release-1", "deployed", 0, {"host": "a"}), ("release-1 deployed", "a", "low")),
        (Event("release-1", "deploy_failed", 1, {"host": "b", "step": "doctor"}), ("release-1 deploy failed", "b failed at doctor", "critical")),
        (Event("release-1", "released", 2, {"version": "1"}), ("release-1 released", "1", "normal")),
    ],
)
def test_each_deploy_event_becomes_one_notice(event, expected):
    note = cli._release_notification(event)
    assert (note.title, note.body, note.urgency) == expected


def test_notify_sends_the_notify_send_argv_and_prints_when_it_is_missing(monkeypatch, capsys):
    event = Event("release-1", "released", 2, {"version": "1"})
    sent: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(cli.subprocess, "run", sent.append)
    cli._release_notify(event)
    assert sent == [["notify-send", "-u", "normal", "-a", "cox", "release-1 released", "1"]]
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    cli._release_notify(event)
    assert capsys.readouterr().out == "[normal] release-1 released: 1\n"
    assert len(sent) == 1
