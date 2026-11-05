import argparse
import datetime
import socket

import pytest

from agent_tools import chair, cli

NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.UTC)
HOST = socket.gethostname()
PID = 4242


def _record(**over):
    return {"session": "chair", "pid": PID, "host": HOST, "taken_at": NOW.isoformat(), "heartbeat_at": NOW.isoformat(), "runs": [], "claude_session": None, **over}


def _setup(tmp_path, monkeypatch, record, caller_pid=PID):
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\n")
    runs = tmp_path / "runs"
    if record is not None:
        chair.write(runs, record)
    monkeypatch.setenv("CLAUDE_PID", str(caller_pid))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(chair, "pid_alive", lambda pid: True)
    return argparse.Namespace(profile=str(profile)), runs


def test_lend_routine_sets_the_key():
    assert chair.lend_routine({"pid": 1}, True) == {"pid": 1, chair.LENT_KEY: True}


def test_lend_routine_removes_the_key():
    assert chair.lend_routine({"pid": 1, chair.LENT_KEY: True}, False) == {"pid": 1}


def test_lend_routine_returns_a_new_dict_and_leaves_its_argument():
    lent = {"pid": 1, chair.LENT_KEY: True}
    plain = {"pid": 1}
    assert chair.lend_routine(plain, True) is not plain
    assert chair.lend_routine(lent, False) is not lent
    assert plain == {"pid": 1}
    assert lent == {"pid": 1, chair.LENT_KEY: True}


def test_lent_key_is_the_agreed_literal():
    assert chair.LENT_KEY == "routine_lent"


def test_a_fresh_take_carries_no_lent_key():
    lent = _record(**{chair.LENT_KEY: True}, until=(NOW - datetime.timedelta(hours=1)).isoformat())
    taken, _ = chair.take(lent, "new", 7, HOST, NOW, 10, False, steal=True)
    assert taken is not None
    assert chair.LENT_KEY not in taken


def test_live_holder_lends_and_recalls(tmp_path, monkeypatch, capsys):
    ns, runs = _setup(tmp_path, monkeypatch, _record())
    assert cli._chair_attach(argparse.Namespace(**vars(ns), lend_routine=True, recall_routine=False), None, None, lambda: NOW) == 0
    assert chair.read(runs)[chair.LENT_KEY] is True
    assert "lent" in capsys.readouterr().out
    assert cli._chair_attach(argparse.Namespace(**vars(ns), lend_routine=False, recall_routine=True), None, None, lambda: NOW) == 0
    assert chair.LENT_KEY not in chair.read(runs)
    assert "recalled" in capsys.readouterr().out


def test_non_holder_is_refused_and_the_file_is_unchanged(tmp_path, monkeypatch, capsys):
    ns, runs = _setup(tmp_path, monkeypatch, _record(), caller_pid=PID + 1)
    before = chair.chair_path(runs).read_bytes()
    assert cli._chair_attach(argparse.Namespace(**vars(ns), lend_routine=True, recall_routine=False), None, None, lambda: NOW) == 1
    assert chair.chair_path(runs).read_bytes() == before
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_stale_record_is_refused_and_the_file_is_unchanged(tmp_path, monkeypatch):
    stale = _record(heartbeat_at=(NOW - datetime.timedelta(hours=2)).isoformat())
    ns, runs = _setup(tmp_path, monkeypatch, stale)
    before = chair.chair_path(runs).read_bytes()
    assert cli._chair_attach(argparse.Namespace(**vars(ns), lend_routine=True, recall_routine=False), None, None, lambda: NOW) == 1
    assert chair.chair_path(runs).read_bytes() == before


def test_no_record_is_refused(tmp_path, monkeypatch):
    ns, runs = _setup(tmp_path, monkeypatch, None)
    assert cli._chair_attach(argparse.Namespace(**vars(ns), lend_routine=True, recall_routine=False), None, None, lambda: NOW) == 1
    assert not chair.chair_path(runs).exists()


def test_attach_without_a_flag_leaves_chair_json_byte_identical(tmp_path, monkeypatch):
    ns, runs = _setup(tmp_path, monkeypatch, _record())
    before = chair.chair_path(runs).read_bytes()
    cli._chair_attach(argparse.Namespace(**vars(ns), lend_routine=False, recall_routine=False), lambda _runs: None, lambda pid: True, lambda: NOW)
    assert chair.chair_path(runs).read_bytes() == before


def test_the_two_flags_are_mutually_exclusive():
    parser = cli.build_parser()
    assert parser.parse_args(["chair", "attach", "--lend-routine"]).lend_routine is True
    assert parser.parse_args(["chair", "attach", "--recall-routine"]).recall_routine is True
    with pytest.raises(SystemExit):
        parser.parse_args(["chair", "attach", "--lend-routine", "--recall-routine"])
