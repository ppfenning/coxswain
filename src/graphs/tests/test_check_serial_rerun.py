"""A pytest check that fails some tests is rerun once on those ids alone, serially."""

from __future__ import annotations

from pathlib import Path

from harness.checks import (
    checks_evidence,
    failed_test_ids,
    flaky_note,
    quarantine_reason,
    run_checks,
    serial_rerun_cmd,
)

IDS = ["tests/test_a.py::test_one", "tests/test_b.py::test_two[x y]"]
OUTPUT = (
    "=== short test summary info ===\n"
    "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
    "FAILED tests/test_b.py::test_two[x y]\n"
    "2 failed, 8 passed in 1.00s\n"
)


def test_failed_ids_come_from_failed_lines_and_an_error_line_forbids_a_rerun() -> None:
    assert failed_test_ids(OUTPUT) == IDS
    assert failed_test_ids(OUTPUT + "ERROR tests/test_c.py - ImportError\n") == []
    assert failed_test_ids("1 failed\n") == []


def test_the_rerun_command_keeps_the_prefix_and_names_only_the_failed_ids() -> None:
    cmd = serial_rerun_cmd("cd src/tools && pytest -q -n auto tests/other", IDS[:1])
    assert cmd == "cd src/tools && pytest -q -n 0 tests/test_a.py::test_one"
    assert serial_rerun_cmd("python -m pytest -x", IDS[:1]) == "python -m pytest -q -n 0 tests/test_a.py::test_one"
    assert serial_rerun_cmd("make test", IDS) is None
    assert serial_rerun_cmd("pytest -q", []) is None


def _fake_pytest(tmp_path: Path, second_run: str) -> str:
    """A `pytest` that fails two ids on its first call, then does `second_run`; every argv is logged."""
    script = tmp_path / "pytest"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {tmp_path}/argv.log\n'
        f"if [ -e {tmp_path}/ran ]; then {second_run}; fi\n"
        f"touch {tmp_path}/ran\n"
        f"printf '%s' '{OUTPUT}'\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    return f"{script} -q -n auto tests"


def test_a_check_that_fails_once_then_passes_counts_as_passed_with_the_note(tmp_path: Path) -> None:
    cmd = _fake_pytest(tmp_path, "echo 2 passed; exit 0")
    [result] = run_checks(tmp_path, [{"name": "t", "cmd": cmd}])
    assert result["passed"] is True
    assert result["exit_code"] == 0
    assert result["flaky"] == IDS
    assert result["note"] == flaky_note(IDS) == f"flaky: {', '.join(IDS)} passed on serial rerun"
    assert quarantine_reason([result]) is None
    assert checks_evidence([result])[0]["output"].endswith(result["note"])


def test_the_rerun_runs_once_and_names_only_the_failed_ids(tmp_path: Path) -> None:
    run_checks(tmp_path, [{"name": "t", "cmd": _fake_pytest(tmp_path, "exit 0")}])
    first, second = (tmp_path / "argv.log").read_text().splitlines()
    assert first == "-q -n auto tests"
    assert second == f"-q -n 0 {IDS[0]} {IDS[1]}"  # echo drops the quotes that kept `x y` one argument


def test_a_test_that_still_fails_on_the_rerun_quarantines(tmp_path: Path) -> None:
    cmd = _fake_pytest(tmp_path, f"printf '%s' '{OUTPUT}'; exit 1")
    [result] = run_checks(tmp_path, [{"name": "t", "cmd": cmd}])
    assert result["passed"] is False
    assert "flaky" not in result
    assert quarantine_reason([result]).startswith("configured check failed: t")
    assert len((tmp_path / "argv.log").read_text().splitlines()) == 2
