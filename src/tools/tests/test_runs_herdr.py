from __future__ import annotations

import io
import stat

import pytest

from agent_tools import runs_herdr

_ROW = {"phase": "build", "node": "coder", "cost_usd": 1.0}
_RUNNING = {**_ROW, "alive": True, "status": "running"}
_BUDGET = {**_ROW, "alive": True, "status": "budget"}
_EXITED = {**_ROW, "alive": False, "status": "exited"}


def _fake_herdr(tmp_path, code=0):
    log = tmp_path / "argv.log"
    script = tmp_path / "herdr"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit {code}\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def _env(script, **over):
    return {"HERDR_ENV": "1", "HERDR_PANE_ID": "p7", "HERDR_BIN_PATH": str(script), **over}


def _calls(log):
    return log.read_text().splitlines() if log.exists() else []


def _agent_calls(log):
    return [c for c in _calls(log) if c.startswith("pane report-agent")]


class _Clock:
    """Each sleep advances ten seconds whatever the interval, so a long interval tests a quiet poll."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, _seconds):
        self.t += 10.0


def _loop(rows_seq, env, *, once=False, interval=10.0, polls=None, err=None, stranded=0, seq_start=0):
    """Feed one rows value per poll, then raise KeyboardInterrupt from sleep to end the loop."""
    clock, feed = _Clock(), iter(rows_seq)
    left = [len(rows_seq) if polls is None else polls]

    def sleep(s):
        left[0] -= 1
        if left[0] <= 0:
            raise KeyboardInterrupt
        clock.sleep(s)

    return runs_herdr.run_loop(
        lambda: next(feed),
        lambda: stranded,
        env,
        once=once,
        interval=interval,
        now=clock.now,
        sleep=sleep,
        err=err or io.StringIO(),
        seq_start=seq_start,
    )


@pytest.mark.parametrize(
    "cls,state", [("running", "working"), ("attention", "blocked"), ("idle", "idle"), ("odd", "idle")]
)
def test_herdr_state_maps_bar_class(cls, state):
    assert runs_herdr.herdr_state(cls) == state


def test_label_counts_in_flight_and_attention():
    assert runs_herdr.label([_RUNNING, _BUDGET, _EXITED]) == "2 in flight, 1 need attention"
    assert runs_herdr.label([_RUNNING]) == "1 in flight"


def test_decide_sends_on_change_or_after_a_full_interval():
    assert runs_herdr.decide(None, "idle", None, 0, 10) is True
    assert runs_herdr.decide("idle", "working", 5, 6, 10) is True
    assert runs_herdr.decide("idle", "idle", 0, 9, 10) is False
    assert runs_herdr.decide("idle", "idle", 0, 10, 10) is True


@pytest.mark.parametrize("missing", ["HERDR_ENV", "HERDR_PANE_ID", "HERDR_BIN_PATH"])
def test_unset_env_is_a_one_line_noop(tmp_path, missing):
    script, log = _fake_herdr(tmp_path)
    env = _env(script)
    del env[missing]
    err = io.StringIO()
    assert _loop([[_RUNNING]], env, once=True, err=err) == 0
    assert len(err.getvalue().splitlines()) == 1
    assert _calls(log) == []


def test_once_reports_then_releases_with_coxswain_as_agent(tmp_path):
    script, log = _fake_herdr(tmp_path)
    assert _loop([[_RUNNING]], _env(script), once=True) == 0
    calls = _calls(log)
    assert calls[0] == "pane report-agent p7 --source coxswain --agent coxswain --state working --seq 1"
    assert calls[-1] == "pane release-agent p7 --source coxswain"
    assert not any("claude" in c for c in calls)


def test_seq_strictly_increases_and_unchanged_state_is_not_resent(tmp_path):
    script, log = _fake_herdr(tmp_path)
    rows = [[_RUNNING], [_RUNNING], [_BUDGET], [_EXITED]]
    _loop(rows, _env(script), interval=1000.0)
    sent = _agent_calls(log)
    assert [c.split("--state ")[1] for c in sent] == ["working --seq 1", "blocked --seq 2", "idle --seq 3"]


def test_keepalive_resends_once_the_interval_has_passed(tmp_path):
    script, log = _fake_herdr(tmp_path)
    _loop([[_RUNNING]] * 3, _env(script), interval=10.0)
    seqs = [int(c.rsplit(" ", 1)[1]) for c in _agent_calls(log)]
    assert seqs == [1, 2, 3]


def test_release_runs_when_the_loop_is_interrupted(tmp_path):
    script, log = _fake_herdr(tmp_path)
    _loop([[_RUNNING]] * 2, _env(script))
    assert _calls(log)[-1] == "pane release-agent p7 --source coxswain"


def test_release_runs_on_sigterm_style_systemexit(tmp_path):
    script, log = _fake_herdr(tmp_path)

    def sleep(_):
        raise SystemExit(0)

    with pytest.raises(SystemExit):
        runs_herdr.run_loop(
            lambda: [_RUNNING],
            lambda: 0,
            _env(script),
            once=False,
            interval=10,
            now=lambda: 0.0,
            sleep=sleep,
            err=io.StringIO(),
        )
    assert _calls(log)[-1] == "pane release-agent p7 --source coxswain"


def test_failing_binary_is_logged_and_does_not_stop_the_loop(tmp_path):
    script, log = _fake_herdr(tmp_path, code=1)
    err = io.StringIO()
    _loop([[_RUNNING]] * 3, _env(script), interval=10.0, err=err)
    assert len(_agent_calls(log)) == 3
    assert "report-agent failed" in err.getvalue()


def test_stranded_work_is_blocked_even_when_bar_says_idle():
    assert runs_herdr.fleet_state("idle", 1) == "blocked"
    assert runs_herdr.fleet_state("running", 2) == "blocked"
    assert runs_herdr.fleet_state("idle", 0) == "idle"
    assert runs_herdr.label([_RUNNING], 1) == "1 in flight, 1 to land"


def test_loop_reports_blocked_for_an_approved_run_waiting_to_land(tmp_path):
    script, log = _fake_herdr(tmp_path)
    _loop([[_EXITED]], _env(script), once=True, stranded=1)
    assert _agent_calls(log) == ["pane report-agent p7 --source coxswain --agent coxswain --state blocked --seq 1"]
    assert "pane report-metadata p7 --source coxswain --label 0 in flight, 1 to land" in _calls(log)


def test_seq_start_seeds_the_counter(tmp_path):
    script, log = _fake_herdr(tmp_path)
    _loop([[_RUNNING]], _env(script), once=True, seq_start=1000)
    assert _agent_calls(log)[0].endswith("--seq 1001")


def test_refused_label_is_tried_once_and_not_again(tmp_path):
    log = tmp_path / "argv.log"
    script = tmp_path / "herdr"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\ncase "$2" in report-metadata) exit 2;; esac\nexit 0\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    err = io.StringIO()
    _loop([[_RUNNING], [_EXITED], [_RUNNING]], _env(script), err=err)
    assert len([c for c in _calls(log) if "report-metadata" in c]) == 1
    assert len(_agent_calls(log)) >= 3
    assert "report-metadata" not in err.getvalue()


def test_no_label_is_sent_when_the_agent_report_failed(tmp_path):
    script, log = _fake_herdr(tmp_path, code=1)
    _loop([[_RUNNING]], _env(script), once=True)
    assert not [c for c in _calls(log) if "report-metadata" in c]


def test_exit_handlers_turn_sigterm_and_sighup_into_systemexit():
    import signal

    seen = {}
    runs_herdr.install_exit_handlers(lambda sig, handler: seen.update({sig: handler}))
    assert set(seen) == {signal.SIGTERM, signal.SIGHUP}
    with pytest.raises(SystemExit):
        seen[signal.SIGHUP](signal.SIGHUP, None)


def test_cli_entry_is_a_noop_without_herdr_env(monkeypatch, capsys, tmp_path):
    import argparse

    from agent_tools import cli

    for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_BIN_PATH"):
        monkeypatch.delenv(key, raising=False)
    assert cli._runs_herdr(argparse.Namespace(runs_dir=str(tmp_path / "runs"), interval=10.0, once=True)) == 0
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_cli_entry_reports_idle_then_releases_through_the_fake_binary(monkeypatch, tmp_path):
    import argparse

    from agent_tools import cli

    script, log = _fake_herdr(tmp_path)
    for key, value in _env(script).items():
        monkeypatch.setenv(key, value)
    (tmp_path / "runs").mkdir()
    assert cli._runs_herdr(argparse.Namespace(runs_dir=str(tmp_path / "runs"), interval=10.0, once=True)) == 0
    calls = _calls(log)
    assert " --state idle --seq " in calls[0]
    assert calls[-1] == "pane release-agent p7 --source coxswain"


def test_missing_binary_is_logged_and_does_not_stop_the_loop(tmp_path):
    err = io.StringIO()
    env = _env(tmp_path / "absent")
    assert _loop([[_RUNNING]] * 2, env, interval=10.0, err=err) == 0
    assert "report-agent failed" in err.getvalue()
