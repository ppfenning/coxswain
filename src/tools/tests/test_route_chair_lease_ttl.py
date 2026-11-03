import datetime
import os
import socket
from argparse import Namespace

import pytest

from agent_tools import chair, cli, store_cli
from agent_tools.cli import main

_HOST = socket.gethostname()
_PID = os.getpid()
_FOUR_HOURS = 4 * 3600


class Store:
    """Recording fakes for the store lease: every acquire and renew is kept as a dict."""

    def __init__(self) -> None:
        self.acquired: list[dict] = []
        self.renewed: list[dict] = []

    def acquire(self, _runs_dir, _name, holder, ttl, steal=False):
        self.acquired.append({"holder": holder, "ttl": ttl, "steal": steal})
        return store_cli.LeaseGranted(7, holder)

    def renew(self, _runs_dir, _name, holder, epoch, ttl):
        self.renewed.append({"holder": holder, "epoch": epoch, "ttl": ttl})
        return store_cli.LeaseGranted(epoch, holder)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    fake = Store()
    monkeypatch.setattr(store_cli, "lease_acquire", fake.acquire)
    monkeypatch.setattr(store_cli, "lease_renew", fake.renew)
    return fake


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(f"workspace_dir: {tmp_path}\n")
    return path


def _argv(profile, verb, *extra):
    return ["route", "chair", verb, "--profile", str(profile), "--label", "bob", "--pid", str(_PID), *extra]


def _hold(tmp_path, session="bob", **extra):
    fresh = datetime.datetime.now(datetime.UTC).isoformat()
    record = {"session": session, "pid": _PID, "host": _HOST, "taken_at": fresh, "heartbeat_at": fresh, "runs": [], **extra}
    chair.write(tmp_path / "runs", record)


def _until(tmp_path) -> datetime.datetime:
    return datetime.datetime.fromisoformat(chair.read(tmp_path / "runs")["until"])


def test_take_with_hours_asks_the_store_for_a_lease_that_reaches_until(tmp_path, profile, store):
    assert main(_argv(profile, "take", "--hours", "4")) == 0
    written = chair.read(tmp_path / "runs")
    [call] = store.acquired
    assert call["ttl"] >= _FOUR_HOURS
    assert call["ttl"] == chair.lease_ttl_for(written, datetime.datetime.fromisoformat(written["taken_at"]))
    assert call["steal"] is False


def test_take_without_hours_asks_for_the_default_ttl(tmp_path, profile, store):
    namespace = Namespace(profile=str(profile), label="bob", pid=_PID, steal=False)
    assert cli._route_chair_take(namespace) == 0
    assert "until" not in chair.read(tmp_path / "runs")
    assert [call["ttl"] for call in store.acquired] == [chair.DEFAULT_LEASE_TTL_SECONDS]


def test_steal_over_a_live_same_holder_record_passes_the_same_long_ttl(tmp_path, profile, store):
    _hold(tmp_path)
    assert main(_argv(profile, "take", "--hours", "4", "--steal")) == 0
    [call] = store.acquired
    assert call["steal"] is True
    assert call["ttl"] >= _FOUR_HOURS
    retaken = chair.read(tmp_path / "runs")
    assert call["ttl"] == chair.lease_ttl_for(retaken, datetime.datetime.fromisoformat(retaken["heartbeat_at"]))


def test_steal_without_hours_keeps_the_recorded_until_in_the_ttl(tmp_path, profile, store):
    until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=6)
    _hold(tmp_path, until=until.isoformat())
    namespace = Namespace(profile=str(profile), label="bob", pid=_PID, steal=True)
    assert cli._route_chair_take(namespace) == 0
    [call] = store.acquired
    assert call["ttl"] >= 6 * 3600 + chair.DEFAULT_LEASE_TTL_SECONDS - 5


def test_beat_by_the_holder_renews_once_with_the_epoch_and_a_ttl_that_reaches_until(tmp_path, profile, store):
    assert main(_argv(profile, "take", "--hours", "4")) == 0
    until = _until(tmp_path)
    before = datetime.datetime.now(datetime.UTC)
    assert main(_argv(profile, "beat")) == 0
    [call] = store.renewed
    assert call["epoch"] == 7
    assert call["ttl"] >= (until - before).total_seconds()
    assert call["ttl"] == chair.lease_ttl_for(chair.read(tmp_path / "runs"), datetime.datetime.fromisoformat(chair.read(tmp_path / "runs")["heartbeat_at"]))


def test_beat_by_a_non_holder_renews_nothing_and_leaves_the_lock(tmp_path, profile, store, capsys):
    _hold(tmp_path, session="alice", until=(datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=4)).isoformat())
    lock = tmp_path / "runs" / "chair.json"
    before = lock.read_bytes()
    assert main(_argv(profile, "beat")) == 2
    assert "not held by bob" in capsys.readouterr().out
    assert store.renewed == []
    assert lock.read_bytes() == before
