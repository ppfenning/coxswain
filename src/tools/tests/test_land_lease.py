import argparse
import inspect
from pathlib import Path

import pytest

from agent_tools import cli, land_lease, store_cli

RUNS = Path("/runs")
GRANTED = store_cli.LeaseGranted(7, "sess-1")


def _fakes(monkeypatch, acquire_result, release_result) -> list:
    calls = []

    def acquire(runs_dir, name, holder, ttl):
        calls.append(("acquire", runs_dir, name, holder, ttl))
        if isinstance(acquire_result, Exception):
            raise acquire_result
        return acquire_result

    def release(runs_dir, name, holder, epoch):
        calls.append(("release", runs_dir, name, holder, epoch))
        if isinstance(release_result, Exception):
            raise release_result
        return release_result

    monkeypatch.setattr(store_cli, "lease_acquire", acquire)
    monkeypatch.setattr(store_cli, "lease_release", release)
    return calls


def _run(body):
    return land_lease.run_under_land_lease(RUNS, "t1", "sess-1", 60, body)


def test_the_fakes_keep_the_real_wrapper_parameter_names():
    assert list(inspect.signature(store_cli.lease_acquire).parameters) == ["runs_dir", "name", "holder", "ttl", "steal"]
    assert list(inspect.signature(store_cli.lease_release).parameters) == ["runs_dir", "name", "holder", "epoch"]


def test_a_normal_exit_acquires_then_releases_the_same_epoch(monkeypatch):
    calls = _fakes(monkeypatch, GRANTED, store_cli.LeaseReleased())
    seen = []
    result = _run(lambda epoch: seen.append(epoch) or "done")
    assert result == land_lease.Ran("done", 7, None)
    assert seen == [7]
    assert calls == [("acquire", RUNS, "land:t1", "sess-1", 60), ("release", RUNS, "land:t1", "sess-1", 7)]


def test_a_body_that_reacquired_moves_the_release_to_its_new_epoch(monkeypatch):
    calls = _fakes(monkeypatch, GRANTED, store_cli.LeaseReleased())
    held = [0]

    def body(epoch):
        held[0] = epoch + 2
        return "done"

    result = land_lease.run_under_land_lease(RUNS, "t1", "sess-1", 60, body, epoch_now=lambda: held[0])
    assert result == land_lease.Ran("done", 9, None)
    assert calls[-1] == ("release", RUNS, "land:t1", "sess-1", 9)


def _walked(monkeypatch, acquires, walk):
    """Runs `cli._land_walked` in store mode over fake store leases. A renew is refused with no holder at its first call, as the
    store does for a lapsed or taken lease, and granted after; `acquires` are the answers to each acquire in turn."""
    calls, answers = [], iter(acquires)

    def acquire(runs_dir, name, holder, ttl, steal=False):
        calls.append(("acquire", name))
        return next(answers)

    def renew(runs_dir, name, holder, epoch, ttl):
        calls.append(("renew", epoch))
        return store_cli.LeaseRefused(None, None) if sum(c[0] == "renew" for c in calls) == 1 else store_cli.LeaseGranted(epoch, holder)

    def release(runs_dir, name, holder, epoch):
        calls.append(("release", epoch))
        return store_cli.LeaseReleased()

    monkeypatch.setattr(store_cli, "lease_acquire", acquire)
    monkeypatch.setattr(store_cli, "lease_renew", renew)
    monkeypatch.setattr(store_cli, "lease_release", release)
    return cli._land_walked(argparse.Namespace(label="me"), RUNS, "store", "t1", lambda: None, walk), calls


def test_the_task_lease_guard_reacquires_an_expired_lease_mid_wait_and_releases_the_new_epoch(monkeypatch, capsys):
    def walk(guard):
        assert guard.beat() is None  # the timer: renew refused, acquire granted at epoch 4
        return (0 if guard("merge") is None else 2), ["merge"], ""

    walked, calls = _walked(monkeypatch, [store_cli.LeaseGranted(3, "me"), store_cli.LeaseGranted(4, "me")], walk)
    assert walked == (0, ["merge"], "")
    assert calls == [("acquire", "land:t1"), ("renew", 3), ("acquire", "land:t1"), ("renew", 4), ("release", 4)]
    assert "land: the land lease for t1 expired with no holder; re-acquired it at epoch 4, continuing" in capsys.readouterr().out


def test_a_task_lease_taken_by_another_holder_mid_wait_stops_the_land_before_merge_but_not_before_other_steps(monkeypatch):
    seen = []

    def walk(guard):
        seen.append(guard.beat())  # the timer: renew refused, acquire names the holder
        seen.append(guard("push"))  # only merge and mark_done are guarded
        seen.append(guard("merge"))
        return (2 if seen[-1] else 0), [], ""

    walked, calls = _walked(monkeypatch, [store_cli.LeaseGranted(3, "me"), store_cli.LeaseRefused(5, "other@h:9")], walk)
    assert walked == (2, [], "")
    assert seen[0] is not None
    assert seen[1] is None
    assert seen[2] == "land: refusing merge, the land lease for t1 could not be renewed: held by other@h:9 at epoch 5"
    assert [c for c in calls if c[0] != "acquire"] == [("renew", 3), ("release", 3)]


@pytest.mark.parametrize("stop", [ValueError("boom"), KeyboardInterrupt(), SystemExit(1)])
def test_an_exception_or_early_stop_still_releases_and_propagates(monkeypatch, stop):
    calls = _fakes(monkeypatch, GRANTED, store_cli.LeaseReleased())

    def body(epoch):
        raise stop

    with pytest.raises(type(stop)) as raised:
        _run(body)
    assert [c[0] for c in calls] == ["acquire", "release"]
    assert getattr(raised.value, "__notes__", []) == []


def test_a_refused_acquire_runs_no_body_and_releases_nothing(monkeypatch):
    calls = _fakes(monkeypatch, store_cli.LeaseRefused(3, "other"), store_cli.LeaseReleased())
    ran = []
    result = _run(ran.append)
    assert result == land_lease.Refused(3, "other")
    assert ran == []
    assert [c[0] for c in calls] == ["acquire"]


@pytest.mark.parametrize("acquired, detail", [
    (store_cli.NotAvailable(), "no harness"),
    (store_cli.LeaseError("exit 2: bad args"), "exit 2: bad args"),
    (OSError("spawn failed"), "OSError: spawn failed"),
])
def test_an_acquire_that_is_not_a_grant_is_unavailable_and_runs_no_body(monkeypatch, acquired, detail):
    calls = _fakes(monkeypatch, acquired, store_cli.LeaseReleased())
    ran = []
    result = _run(ran.append)
    assert result == land_lease.Unavailable(detail)
    assert ran == []
    assert [c[0] for c in calls] == ["acquire"]


def test_a_bug_in_acquire_propagates_instead_of_reading_as_unavailable(monkeypatch):
    calls = _fakes(monkeypatch, TypeError("wrong signature"), store_cli.LeaseReleased())
    ran = []
    with pytest.raises(TypeError, match="wrong signature"):
        _run(ran.append)
    assert ran == []
    assert [c[0] for c in calls] == ["acquire"]


_RELEASE_FAILURES = [
    (store_cli.LeaseError("exit 2: nope"), "exit 2: nope"),
    (store_cli.NotAvailable(), "no harness"),
    (store_cli.LeaseRefused(8, "other"), "held by other at epoch 8"),
    (OSError("gone"), "OSError: gone"),
]


@pytest.mark.parametrize("released, reason", _RELEASE_FAILURES)
def test_a_failing_release_is_a_warning_and_the_body_result_stands(monkeypatch, released, reason):
    _fakes(monkeypatch, GRANTED, released)
    result = _run(lambda epoch: "done")
    assert result == land_lease.Ran("done", 7, f"warning: land lease land:t1 not released: {reason}")


@pytest.mark.parametrize("released, reason", _RELEASE_FAILURES)
def test_a_failing_release_rides_as_a_note_on_the_body_exception(monkeypatch, released, reason):
    _fakes(monkeypatch, GRANTED, released)

    def body(epoch):
        raise ValueError("body failed")

    with pytest.raises(ValueError, match="body failed") as raised:
        _run(body)
    assert raised.value.__notes__ == [f"warning: land lease land:t1 not released: {reason}"]


def test_injected_callables_replace_the_wrappers(monkeypatch):
    _fakes(monkeypatch, store_cli.LeaseError("wrapper used"), store_cli.LeaseError("wrapper used"))
    seen = []
    result = land_lease.run_under_land_lease(
        RUNS, "t1", "h", 5, lambda epoch: epoch,
        acquire=lambda **kw: seen.append(kw) or store_cli.LeaseGranted(2, "h"),
        release=lambda **kw: seen.append(kw) or store_cli.LeaseReleased(),
    )
    assert result == land_lease.Ran(2, 2, None)
    assert seen == [
        {"runs_dir": RUNS, "name": "land:t1", "holder": "h", "ttl": 5},
        {"runs_dir": RUNS, "name": "land:t1", "holder": "h", "epoch": 2},
    ]
