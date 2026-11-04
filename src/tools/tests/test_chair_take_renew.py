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


def _take(tmp_path, label, pid, *extra):
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\n")
    return main(["route", "chair", "take", "--profile", str(profile), "--label", label, "--pid", str(pid), *extra])


def _until(tmp_path):
    return datetime.datetime.fromisoformat(chair.read(tmp_path / "runs")["until"])


def test_same_holder_take_without_steal_renews_until(tmp_path, capsys, lease_calls):
    assert _take(tmp_path, "bob", 5151, "--hours", "1") == 0
    first = _until(tmp_path)
    capsys.readouterr()
    assert _take(tmp_path, "bob", 5151, "--hours", "3") == 0
    assert "chair lease retaken: bob (pid 5151)" in capsys.readouterr().out
    assert _until(tmp_path) > first
    assert lease_calls[-1]["steal"] is False


def test_different_label_without_steal_is_still_refused(tmp_path, capsys, lease_calls):
    assert _take(tmp_path, "alice", 4242, "--hours", "1") == 0
    lock = tmp_path / "runs" / "chair.json"
    before = lock.read_bytes()
    capsys.readouterr()
    assert _take(tmp_path, "bob", 5151, "--hours", "3") == 2
    out = capsys.readouterr().out
    assert "held by alice" in out
    assert "retaken" not in out
    assert lock.read_bytes() == before


def test_loop_beat_extends_until_and_adopts_a_prior_holder_without_one(tmp_path):
    runs = tmp_path / "runs"
    now = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=datetime.UTC)
    taken = now - datetime.timedelta(hours=1)
    held = {
        "session": "bob",
        "pid": 5151,
        "host": _HOST,
        "taken_at": taken.isoformat(),
        "heartbeat_at": taken.isoformat(),
        "runs": [],
        "until": (taken + datetime.timedelta(hours=3)).isoformat(),
        "window_seconds": 10800.0,
    }
    chair.write(runs, held)
    cli._beat_record(runs, "bob", 5151, _HOST, now)
    beaten = chair.read(runs)
    assert datetime.datetime.fromisoformat(beaten["until"]) == now + datetime.timedelta(hours=3)
    assert beaten["heartbeat_at"] == now.isoformat()
    chair.write(runs, {**held, "session": "alice", "pid": 4242})
    cli._beat_record(runs, "bob", 5151, _HOST, now)
    adopted = chair.read(runs)
    assert (adopted["session"], adopted["pid"]) == ("bob", 5151)
    assert "until" not in adopted


@pytest.mark.parametrize(
    "heartbeat_age_minutes, until_offset_hours, refusal",
    [(0, -1, "takeover expired"), (99, None, "stale")],
)
def test_same_holder_take_of_a_lapsed_or_stale_record_without_steal_keeps_its_refusal(
    tmp_path, capsys, lease_calls, heartbeat_age_minutes, until_offset_hours, refusal
):
    now = datetime.datetime.now(datetime.UTC)
    heartbeat = (now - datetime.timedelta(minutes=heartbeat_age_minutes)).isoformat()
    record = {
        "session": "bob",
        "pid": 5151,
        "host": _HOST,
        "taken_at": heartbeat,
        "heartbeat_at": heartbeat,
        "runs": [],
    }
    until = (
        {}
        if until_offset_hours is None
        else {"until": (now + datetime.timedelta(hours=until_offset_hours)).isoformat()}
    )
    chair.write(tmp_path / "runs", {**record, **until})
    lock = tmp_path / "runs" / "chair.json"
    before = lock.read_bytes()
    assert _take(tmp_path, "bob", 5151, "--hours", "3") == 2
    assert refusal in capsys.readouterr().out
    assert lock.read_bytes() == before
    assert lease_calls == []
