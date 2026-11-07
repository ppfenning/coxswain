"""run_epic runs `landing_areas.integration_checks` once per assembled level, on the level's own test database."""

from __future__ import annotations

from pathlib import Path

from harness.epic import _level_integration_reason
from harness.lane_database import database_name, lane_env
from tests.test_epic_driver import cart, drive, git, repo  # noqa: F401
from tests.test_epic_waves import Gated, by_phase, work

VAR = "LANE_DB"
SETTING = {"env": VAR, "template": "postgresql://localhost/{lane}_test"}


def level_db() -> str:
    return database_name(lane_env(SETTING, "epic-1", "pa", "level")[VAR])


def configure(cart, tmp_path: Path, cmd: str, **setting) -> Path:  # noqa: F811
    """Set one integration check and a test database; the create command appends the name to the returned marker."""
    created = tmp_path / "created.marker"
    cart["landing_areas"]["test_database"] = {**SETTING, "create": f"echo {{name}} >> {created}", **setting}
    cart["landing_areas"]["integration_checks"] = [{"name": "integ", "cmd": cmd}]
    return created


def test_a_passing_level_stays_complete_with_rows_scope_and_database(repo, cart, tmp_path) -> None:  # noqa: F811
    ran = tmp_path / "ran.marker"
    created = configure(cart, tmp_path, f'echo "$LANE_DB {{packages}}" >> {ran}')
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    record = by_phase(result)["pa"]
    assert record["status"] == "complete"
    assert [(r["name"], r["passed"]) for r in record["level_integration"]] == [("integ", True)]
    assert record["level_integration_scope"]["packages"] == ["ta-one.txt"]
    assert record["level_integration_scope"]["ran"] == ["integ"]
    assert created.read_text().split() == [level_db()]
    url, packages = ran.read_text().split()
    assert url == lane_env(SETTING, "epic-1", "pa", "level")[VAR] and packages == "ta-one.txt"
    assert level_db() in {r["name"] for r in result["lane_databases"]}


def test_a_failing_integration_check_marks_the_level_partial_and_names_it(repo, cart, tmp_path) -> None:  # noqa: F811
    configure(cart, tmp_path, "echo boom-tail; exit 3")
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    record = by_phase(result)["pa"]
    assert record["status"] == "partial"
    assert record["reason"] == "level pa failed integration check integ"
    assert "boom-tail" in record["level_integration"][0]["output_tail"]
    assert "review_pr" not in record
    assert result["totals"]["phases_complete"] == 0


def test_a_failed_creation_marks_the_level_partial_and_runs_no_check(repo, cart, tmp_path) -> None:  # noqa: F811
    ran = tmp_path / "ran.marker"
    configure(cart, tmp_path, f"touch {ran}", create="echo nope >&2; exit 1", drop="dropdb {name}")
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    record = by_phase(result)["pa"]
    assert record["status"] == "partial"
    assert record["reason"] == f"level pa: lane database {level_db()} not created: nope"
    assert record["level_integration"] == []
    assert not ran.exists()
    assert level_db() in {r["name"] for r in result["lane_databases"]}


def test_the_level_database_is_dropped_with_the_run_and_listed(repo, cart, tmp_path) -> None:  # noqa: F811
    configure(cart, tmp_path, "true")
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    listed = {r["name"]: r["status"] for r in result["lane_databases"]}
    assert listed[level_db()] == "pending"


def test_a_failing_level_check_stops_the_level_before_the_integration_gate(repo, cart, tmp_path) -> None:  # noqa: F811
    created = configure(cart, tmp_path, "true")
    cart["landing_areas"]["level_checks"] = [{"name": "level", "cmd": "exit 1"}]
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    record = by_phase(result)["pa"]
    assert record["reason"] == "level pa failed level check level"
    assert "level_integration" not in record and not created.exists()


def test_without_integration_checks_the_record_and_the_database_calls_are_unchanged(repo, cart, tmp_path) -> None:  # noqa: F811
    created = tmp_path / "created.marker"
    cart["landing_areas"]["test_database"] = {**SETTING, "create": f"echo {{name}} >> {created}"}
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    record = by_phase(result)["pa"]
    assert record["status"] == "complete"
    assert "level_integration" not in record and "level_integration_scope" not in record
    assert not created.exists()
    assert level_db() not in {r["name"] for r in result["lane_databases"]}


def test_integration_checks_without_a_test_database_run_nothing(repo, cart, tmp_path) -> None:  # noqa: F811
    ran = tmp_path / "ran.marker"
    cart["landing_areas"]["integration_checks"] = [{"name": "integ", "cmd": f"touch {ran}"}]
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    record = by_phase(result)["pa"]
    assert record["status"] == "complete" and "level_integration" not in record
    assert not ran.exists()


def test_the_reason_names_the_level_and_the_first_stop() -> None:
    rows = [{"name": "a", "passed": True}, {"name": "b", "passed": False}, {"name": "c", "passed": False}]
    created = {"name": "db", "status": "created", "detail": ""}
    failed = {"name": "db", "status": "failed", "detail": "no server\nmore"}
    assert _level_integration_reason("pa", created, rows) == "level pa failed integration check b"
    assert _level_integration_reason("pa", created, rows[:1]) == ""
    assert _level_integration_reason("pa", failed, []) == "level pa: lane database db not created: no server"
