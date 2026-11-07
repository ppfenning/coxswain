"""`create_lane_database` returns a record for every outcome and runs through an injected runner."""

from __future__ import annotations

import subprocess

from harness.lane_database_create import create_lane_database, creation_failure


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


def test_command_text_substitutes_name_and_schema_and_keeps_other_braces():
    run = _FakeRun()
    create_lane_database("createdb {name} && psql -f {schema} -v x={x}", "db/s.sql", "lane_7", {}, "/wt", run)
    cmd, _ = run.calls[0]
    assert cmd == "createdb lane_7 && psql -f db/s.sql -v x={x}"


def test_cwd_shell_and_env_overrides_are_passed():
    run = _FakeRun()
    create_lane_database("createdb {name}", "s.sql", "lane_7", {"PGHOST": "db"}, "/wt", run)
    _, kwargs = run.calls[0]
    assert kwargs["cwd"] == "/wt"
    assert kwargs["shell"] is True
    assert kwargs["env"]["PGHOST"] == "db"
    assert kwargs["timeout"] > 0


def test_exit_zero_is_created():
    record = create_lane_database("createdb {name}", "s.sql", "lane_7", {}, "/wt", _FakeRun(stdout="CREATE"))
    assert record == {"name": "lane_7", "status": "created", "detail": "CREATE"}


def test_non_zero_is_failed_with_the_output_in_detail():
    run = _FakeRun(returncode=2, stderr="bad schema")
    record = create_lane_database("createdb {name}", "s.sql", "lane_7", {}, "/wt", run)
    assert record["status"] == "failed"
    assert "bad schema" in record["detail"]


def test_non_zero_with_no_output_names_the_exit_code():
    record = create_lane_database("x", "s.sql", "lane_7", {}, "/wt", _FakeRun(returncode=3))
    assert record["detail"] == "exit 3"


def test_a_raised_oserror_is_failed_not_an_exception():
    run = _FakeRun(raises=OSError("no shell"))
    record = create_lane_database("createdb {name}", "s.sql", "lane_7", {}, "/wt", run)
    assert record["status"] == "failed"
    assert "no shell" in record["detail"]


def test_a_timeout_is_failed_not_an_exception():
    run = _FakeRun(raises=subprocess.TimeoutExpired("createdb", 60))
    assert create_lane_database("createdb {name}", "s.sql", "lane_7", {}, "/wt", run)["status"] == "failed"


def test_no_create_cmd_is_skipped_and_runs_nothing():
    for empty in (None, ""):
        run = _FakeRun()
        record = create_lane_database(empty, "s.sql", "lane_7", {}, "/wt", run)
        assert record["name"] == "lane_7"
        assert record["status"] == "skipped"
        assert "no create command was configured" in record["detail"]
        assert run.calls == []


def test_creation_failure_is_a_one_line_reason_only_for_failed():
    failed = {"name": "lane_7", "status": "failed", "detail": "first\nsecond"}
    reason = creation_failure(failed)
    assert reason == "lane database lane_7 not created: first"
    assert "\n" not in reason
    assert creation_failure({"name": "lane_7", "status": "created", "detail": ""}) == ""
    assert creation_failure({"name": "lane_7", "status": "skipped", "detail": "x"}) == ""


def test_creation_failure_with_empty_detail_still_gives_a_reason():
    assert creation_failure({"name": "lane_7", "status": "failed", "detail": ""}) != ""
