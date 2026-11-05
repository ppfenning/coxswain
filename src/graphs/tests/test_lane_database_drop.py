"""`drop_lane_database` returns a record for every outcome and runs through an injected runner."""

from __future__ import annotations

import subprocess

from harness.lane_database_drop import drop_lane_database


class _FakeRun:
    """Records each call; returns a canned result or raises the canned error."""

    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.calls = []
        self._result = subprocess.CompletedProcess("", returncode, stdout, stderr)
        self._raises = raises

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result


def test_command_text_carries_the_substituted_name_and_env_overrides():
    run = _FakeRun()
    drop_lane_database("dropdb {name} --if-exists {x}", "lane_7", {"PGHOST": "db"}, run)
    cmd, kwargs = run.calls[0]
    assert cmd == "dropdb lane_7 --if-exists {x}"
    assert kwargs["shell"] is True
    assert kwargs["env"]["PGHOST"] == "db"


def test_exit_zero_is_dropped():
    record = drop_lane_database("dropdb {name}", "lane_7", {}, _FakeRun(stdout="DROP DATABASE"))
    assert record == {"name": "lane_7", "status": "dropped", "detail": "DROP DATABASE"}


def test_non_zero_is_failed_with_the_output_in_detail():
    record = drop_lane_database("dropdb {name}", "lane_7", {}, _FakeRun(returncode=2, stderr="in use"))
    assert record["status"] == "failed"
    assert "in use" in record["detail"]


def test_a_raised_oserror_is_failed_not_an_exception():
    record = drop_lane_database("dropdb {name}", "lane_7", {}, _FakeRun(raises=OSError("no shell")))
    assert record["status"] == "failed"
    assert "no shell" in record["detail"]


def test_a_timeout_is_failed_not_an_exception():
    run = _FakeRun(raises=subprocess.TimeoutExpired("dropdb", 60))
    assert drop_lane_database("dropdb {name}", "lane_7", {}, run)["status"] == "failed"


def test_no_drop_cmd_is_pending_and_runs_nothing():
    run = _FakeRun()
    record = drop_lane_database(None, "lane_7", {}, run)
    assert record["name"] == "lane_7"
    assert record["status"] == "pending"
    assert run.calls == []
