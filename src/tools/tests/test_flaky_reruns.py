"""The flaky note reaches the land's `checks:` line and `runs detail`; `stats flaky` lists repeat offenders."""

from __future__ import annotations

import datetime
from pathlib import Path

from agent_tools import cli, land, runs_detail, stats_flaky

NOTE = "flaky: tests/test_a.py::test_one passed on serial rerun"
TODAY = datetime.date(2026, 10, 30)


def _record(*ids: str) -> dict:
    return {"change_facts": {"checks": [{"name": "t", "passed": True, "flaky": list(ids), "note": NOTE}]}}


def test_the_lands_checks_detail_carries_the_flaky_note_after_the_head() -> None:
    assert land.checks_detail("1 checks passed", [], [NOTE]) == f"1 checks passed\nchecks: {NOTE}"


def test_a_land_check_that_passes_only_on_the_serial_rerun_prints_the_note(tmp_path: Path) -> None:
    fake = tmp_path / "pytest"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = "-n" ] && [ "$3" = "0" ]; then exit 0; fi\n'
        "echo 'FAILED tests/test_a.py::test_one - boom'\nexit 1\n"
    )
    fake.chmod(0o755)
    ok, detail = cli._run_checks([("tests", f"{fake} -q -n auto")], tmp_path)
    assert ok
    assert detail.splitlines() == ["1 checks passed", f"checks: {NOTE}"]


def test_runs_detail_renders_the_flaky_note() -> None:
    lines = runs_detail.render(runs_detail.detail("r1", False, [], [], _record("tests/test_a.py::test_one"), []), 200)
    assert NOTE in lines


def test_a_test_rerun_three_times_in_a_week_is_flaky_and_the_rest_are_not() -> None:
    a, b = "tests/test_a.py::test_one", "tests/test_b.py::test_two"
    dated = [
        ("2026-10-30", _record(a)),
        ("2026-10-28", _record(a, b)),
        ("2026-10-24", _record(a, b)),  # the 7-day window is 10-24 to 10-30
        ("2026-10-23", _record(b)),  # outside it
    ]
    assert stats_flaky.flaky_tests(dated, TODAY) == {a: 3}
    assert stats_flaky.render_lines({a: 3}) == ["flaky (serial reruns in 7 days):", f"  3  {a}"]
    assert stats_flaky.flaky_tests(dated[:2], TODAY) == {}
