import datetime
import socket

import pytest

from agent_tools import chair, cli
from agent_tools.cli import main

_HOST = socket.gethostname()


@pytest.fixture
def lease_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(chair, "acquire_lease", lambda *args, **kwargs: calls.append(kwargs) or "")
    monkeypatch.setattr(chair, "pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_leader_pid_alive", lambda record: True)
    return calls


def _holder(tmp_path, heartbeat_age_minutes=0):
    heartbeat = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=heartbeat_age_minutes)).isoformat()
    record = {
        "session": "alice",
        "pid": 4242,
        "host": _HOST,
        "taken_at": heartbeat,
        "heartbeat_at": heartbeat,
        "runs": [],
    }
    chair.write(tmp_path / "runs", record)
    return record


def _take(tmp_path, *extra):
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\n")
    return main(["route", "chair", "take", "--profile", str(profile), "--label", "bob", "--pid", "5151", *extra])


def test_steal_over_a_live_holder_writes_the_thief_and_names_the_displaced(tmp_path, capsys, lease_calls):
    old = _holder(tmp_path)
    assert _take(tmp_path, "--steal") == 0
    out = capsys.readouterr().out
    assert chair.read(tmp_path / "runs")["session"] == "bob"
    assert chair.displaced_line(old) in out
    assert out.index(chair.displaced_line(old)) < out.index("chair taken: bob")


def test_steal_over_a_live_holder_asks_the_store_lease_to_steal(tmp_path, lease_calls):
    _holder(tmp_path)
    assert _take(tmp_path, "--steal") == 0
    assert lease_calls == [{"steal": True}]


def test_without_steal_a_live_holder_is_refused_and_the_lock_is_unchanged(tmp_path, capsys, lease_calls):
    _holder(tmp_path)
    lock = tmp_path / "runs" / "chair.json"
    before = lock.read_bytes()
    assert _take(tmp_path) == 2
    out = capsys.readouterr().out
    assert "held by alice" in out
    assert "displaced" not in out
    assert lock.read_bytes() == before
    assert lease_calls == []


def test_steal_over_a_stale_holder_takes_without_naming_a_displaced_chair(tmp_path, capsys, lease_calls):
    _holder(tmp_path, heartbeat_age_minutes=99)
    assert _take(tmp_path, "--steal") == 0
    out = capsys.readouterr().out
    assert chair.read(tmp_path / "runs")["session"] == "bob"
    assert "displaced" not in out
    assert "chair taken: bob" in out
