from __future__ import annotations

import dataclasses
import datetime
import io
import re
import stat
import subprocess

import pytest
from test_run_store import lane_run, lane_store

from agent_tools import runs_herdr
from agent_tools.runs_top_screen import lost_rows, rows_now

_ROW = {"phase": "build", "node": "coder", "cost_usd": 1.0}
_RUNNING = {**_ROW, "alive": True, "status": "running"}
_BUDGET = {**_ROW, "alive": True, "status": "budget"}
_EXITED = {**_ROW, "alive": False, "status": "exited"}
_RELEASE = "pane release-agent p7 --source coxswain --agent coxswain"


_FAKE_HERDR = """#!/bin/sh
# Modelled on the ticket's account of herdr 0.9.5, not on its --help output; check against `herdr pane <cmd> --help`.
#   pane report-agent <pane> --source S --agent A --state ST [--seq N] [--message M]
#   pane report-metadata <pane> [--source S] [--title T] [--display-agent D] [--applies-to-source S]
#   pane release-agent <pane> --source S --agent A
echo "$@" >> "{log}"
case "$2" in
  report-agent) allowed=" --source --agent --state --seq --message "; required="--source --agent --state" ;;
  report-metadata) allowed=" --source --title --display-agent --applies-to-source "; required="" ;;
  release-agent) allowed=" --source --agent "; required="--source --agent" ;;
  *) echo "unknown command $2" >&2; exit 2 ;;
esac
seen=" "
shift 3
while [ $# -gt 0 ]; do
  case "$allowed" in *" $1 "*) ;; *) echo "unknown flag $1" >&2; exit 2 ;; esac
  seen="$seen$1 "
  shift 2
done
for f in $required; do
  case "$seen" in *" $f "*) ;; *) echo "missing required $f" >&2; exit 2 ;; esac
done
exit {code}
"""


def _fake_herdr(tmp_path, code=0):
    log = tmp_path / "argv.log"
    script = tmp_path / "herdr"
    script.write_text(_FAKE_HERDR.format(log=log, code=code))
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
    assert runs_herdr.decide(None, "idle", None, "a", None, 0, 10) is True
    assert runs_herdr.decide("idle", "working", "a", "a", 5, 6, 10) is True
    assert runs_herdr.decide("idle", "idle", "a", "b", 5, 6, 10) is True
    assert runs_herdr.decide("idle", "idle", "a", "a", 0, 9, 10) is False
    assert runs_herdr.decide("idle", "idle", "a", "a", 0, 10, 10) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["pane", "release-agent", "p7", "--source", "coxswain"],
        ["pane", "report-metadata", "p7", "--source", "coxswain", "--label", "x"],
        ["pane", "report-agent", "p7", "--source", "coxswain", "--agent", "coxswain", "--state", "idle", "--label", "x"],
    ],
)
def test_fake_herdr_rejects_the_argv_the_real_cli_rejects(tmp_path, argv):
    script, _ = _fake_herdr(tmp_path)
    assert subprocess.run([str(script), *argv], capture_output=True, check=False).returncode == 2


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
    assert calls[0] == "pane report-agent p7 --source coxswain --agent coxswain --state working --seq 1 --message 1 in flight"
    assert calls[-1] == _RELEASE
    assert not any("claude" in c for c in calls)


def test_seq_strictly_increases_and_unchanged_state_is_not_resent(tmp_path):
    script, log = _fake_herdr(tmp_path)
    rows = [[_RUNNING], [_RUNNING], [_BUDGET], [_EXITED]]
    _loop(rows, _env(script), interval=1000.0)
    sent = _agent_calls(log)
    assert [c.split("--state ")[1].split(" --message")[0] for c in sent] == ["working --seq 1", "blocked --seq 2", "idle --seq 3"]


def test_keepalive_resends_once_the_interval_has_passed(tmp_path):
    script, log = _fake_herdr(tmp_path)
    _loop([[_RUNNING]] * 3, _env(script), interval=10.0)
    seqs = [int(re.search(r"--seq (\d+)", c).group(1)) for c in _agent_calls(log)]
    assert seqs == [1, 2, 3]


def test_release_runs_when_the_loop_is_interrupted(tmp_path):
    script, log = _fake_herdr(tmp_path)
    _loop([[_RUNNING]] * 2, _env(script))
    assert _calls(log)[-1] == _RELEASE


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
    assert _calls(log)[-1] == _RELEASE


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
    assert _agent_calls(log) == [
        "pane report-agent p7 --source coxswain --agent coxswain --state blocked --seq 1 --message 0 in flight, 1 to land"
    ]


def test_seq_start_seeds_the_counter(tmp_path):
    script, log = _fake_herdr(tmp_path)
    _loop([[_RUNNING]], _env(script), once=True, seq_start=1000)
    assert " --seq 1001 " in _agent_calls(log)[0]


def test_a_label_only_change_sends_a_report_carrying_it_as_message(tmp_path):
    script, log = _fake_herdr(tmp_path)
    err = io.StringIO()
    _loop([[_RUNNING], [_RUNNING, _RUNNING]], _env(script), interval=1000.0, err=err)
    assert [c.split("--state ")[1] for c in _agent_calls(log)] == [
        "working --seq 1 --message 1 in flight",
        "working --seq 2 --message 2 in flight",
    ]
    assert not [c for c in _calls(log) if "report-metadata" in c]
    assert err.getvalue() == ""


def test_a_failed_report_is_retried_with_its_label_on_the_next_poll(tmp_path):
    script, log = _fake_herdr(tmp_path, code=1)
    _loop([[_RUNNING]] * 2, _env(script), interval=1000.0)
    assert len(_agent_calls(log)) == 2


_NOW = datetime.datetime(2026, 9, 25, 6, 0, 0, tzinfo=datetime.UTC)


def _remote_rows(tmp_path, beat, expires="2026-09-25T06:02:00Z"):
    lane_store(tmp_path, [lane_run("x-3", "2026-09-25T05:00:00Z")], [("runs:x", "x-3", expires, beat)], host_column=True)
    return [dataclasses.asdict(r) for r in [*rows_now(tmp_path, now=_NOW), *lost_rows(tmp_path, now=_NOW)]]


def test_a_remote_lane_with_a_fresh_heartbeat_reports_working_with_no_local_runs(tmp_path):
    script, log = _fake_herdr(tmp_path)
    _loop([_remote_rows(tmp_path, "2026-09-25T05:59:50Z")], _env(script), once=True)
    assert "--state working --seq 1 --message 1 in flight" in _agent_calls(log)[0]


def test_a_remote_lane_with_a_stale_heartbeat_reports_blocked(tmp_path):
    script, log = _fake_herdr(tmp_path)
    # A dead host: the last beat was 05:40 and its 120s lease lapsed at 05:42, so `rows_now` alone would not list it.
    _loop([_remote_rows(tmp_path, "2026-09-25T05:40:00Z", "2026-09-25T05:42:00Z")], _env(script), once=True)
    assert "--state blocked --seq 1 --message 1 in flight, 1 need attention" in _agent_calls(log)[0]


def test_a_released_lease_is_not_a_lost_lane(tmp_path):
    assert _remote_rows(tmp_path, "2026-09-25T05:40:00Z", "1970-01-01T00:00:00Z") == []


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
    assert calls[-1] == _RELEASE


def test_missing_binary_is_logged_and_does_not_stop_the_loop(tmp_path):
    err = io.StringIO()
    env = _env(tmp_path / "absent")
    assert _loop([[_RUNNING]] * 2, env, interval=10.0, err=err) == 0
    assert "report-agent failed" in err.getvalue()
