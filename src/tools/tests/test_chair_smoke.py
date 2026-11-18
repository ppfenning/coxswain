import subprocess
from collections.abc import Callable

from agent_tools.chair_smoke import (
    clear_hold,
    hold_record,
    read_hold,
    smoke_result,
    smoke_verdict,
    write_hold,
)

_OK = {"command": ["cox", "route", "context"], "ok": True, "tail": "fine"}


def test_smoke_verdict_all_ok() -> None:
    results = [_OK, dict(_OK), dict(_OK)]
    assert smoke_verdict(results) == (True, None)


def test_smoke_verdict_first_failure() -> None:
    failing = {
        "command": ["cox", "runs", "top", "--once"],
        "ok": False,
        "tail": "Traceback (most recent call last):\nboom",
    }
    results = [_OK, failing, dict(_OK)]
    assert smoke_verdict(results) == (False, failing)


def test_hold_record_caps_tail_and_sets_cause() -> None:
    land = {"repo": "org/repo", "pr": 7, "commit": "deadbeef"}
    failing = {
        "command": ["cox", "chair", "run", "--once", "--dry-run"],
        "ok": False,
        "tail": "\n".join(f"line {i}" for i in range(30)),
    }
    record = hold_record(land, failing)
    assert record["land"] == land
    assert record["land"] is not land
    assert record["failing_command"] == failing["command"]
    assert record["cause"] == "smoke_failed"
    assert record["tail"].splitlines() == [f"line {i}" for i in range(10, 30)]


def test_write_then_read_hold_round_trips(tmp_path) -> None:
    runs_dir = str(tmp_path)
    record = {
        "land": {"repo": "org/repo", "pr": 7, "commit": "deadbeef"},
        "failing_command": ["cox", "route", "context"],
        "tail": "boom",
        "cause": "smoke_failed",
    }
    write_hold(runs_dir, record)
    assert read_hold(runs_dir) == record


def test_clear_hold_then_read_hold_is_none(tmp_path) -> None:
    runs_dir = str(tmp_path)
    write_hold(runs_dir, {"land": {}, "failing_command": [], "tail": "", "cause": "smoke_failed"})
    clear_hold(runs_dir)
    assert read_hold(runs_dir) is None


def test_smoke_result_clean_exit_is_ok() -> None:
    result = smoke_result(["cox", "route", "context"], 0, "all good", timed_out=False)
    assert result == {"command": ["cox", "route", "context"], "ok": True, "tail": "all good", "timed_out": False}


def test_smoke_result_nonzero_exit_is_not_ok() -> None:
    result = smoke_result(["cox", "route", "context"], 1, "boom", timed_out=False)
    assert result["ok"] is False


def test_smoke_result_traceback_in_output_is_not_ok_even_on_exit_0() -> None:
    result = smoke_result(["cox", "route", "context"], 0, "Traceback (most recent call last):\nboom", timed_out=False)
    assert result["ok"] is False


def test_smoke_result_timed_out_is_not_ok_even_on_exit_0_clean_output() -> None:
    result = smoke_result(["cox", "route", "context"], 0, "still running", timed_out=True)
    assert result["ok"] is False
    assert result["timed_out"] is True


def test_smoke_verdict_timeout_is_inconclusive_not_a_failure() -> None:
    timed_out = smoke_result(["cox", "chair", "run", "--once", "--dry-run"], 124, "timed out", timed_out=True)
    assert smoke_verdict([_OK, timed_out]) == (True, None)


def test_smoke_verdict_failure_after_a_timeout_still_fails() -> None:
    timed_out = smoke_result(["cox", "route", "context"], 124, "timed out", timed_out=True)
    failing = smoke_result(["cox", "runs", "top", "--once"], 1, "boom", timed_out=False)
    assert smoke_verdict([timed_out, failing]) == (False, failing)


def _fake_run(calls: list[list[str]], fail_on: str | None) -> Callable[..., None]:
    def run(argv: list[str], check: bool, cwd: str | None = None, timeout: float | None = None) -> None:
        calls.append(argv)
        if fail_on is not None and fail_on in argv:
            raise subprocess.CalledProcessError(1, argv)

    return run
