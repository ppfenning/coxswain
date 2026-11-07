from __future__ import annotations

from typing import Any

import harness.cli as cli
import harness.epic as epic


def test_awaiting_land_count_appears_in_line() -> None:
    line = cli.epic_summary_line("r1", {"phases_awaiting_land": 2})
    assert "2 awaiting land" in line


def test_missing_awaiting_land_key_prints_zero() -> None:
    line = cli.epic_summary_line("r1", {"phases_complete": 1})
    assert "0 awaiting land" in line


def test_other_counts_keep_position_and_wording() -> None:
    totals = {
        "phases_complete": 1,
        "phases_partial": 2,
        "phases_blocked": 3,
        "phases_awaiting_land": 4,
        "tasks_quarantined": 5,
        "stacks_rebased": 6,
    }
    assert cli.epic_summary_line("r9", totals) == (
        "\nepic r9: 1 phase(s) complete, 2 partial, 3 blocked, 4 awaiting land, "
        "5 task(s) quarantined, 6 stack(s) rebased"
    )


def test_line_unchanged_when_no_totals_mention_integration() -> None:
    totals = {"phases_complete": 1, "stacks_rebased": 2}
    assert cli.epic_summary_line("r2", totals) == (
        "\nepic r2: 1 phase(s) complete, 0 partial, 0 blocked, 0 awaiting land, "
        "0 task(s) quarantined, 2 stack(s) rebased"
    )
    assert "integration" not in cli.epic_summary_line("r2", {"integration_tasks_passed": 0})


def test_integration_clause_follows_existing_counts() -> None:
    totals = {
        "stacks_rebased": 6,
        "integration_tasks_passed": 3,
        "integration_tasks_failed": 1,
        "integration_levels_passed": 2,
        "integration_levels_failed": 4,
    }
    assert cli.epic_summary_line("r3", totals).endswith(
        "6 stack(s) rebased; integration: 3 task(s) passed, 1 failed, 2 level(s) passed, 4 failed"
    )


def test_missing_integration_key_prints_zero_in_clause() -> None:
    line = cli.epic_summary_line("r4", {"integration_levels_failed": 1})
    assert line.endswith("; integration: 0 task(s) passed, 0 failed, 0 level(s) passed, 1 failed")


def _row(ok: bool, **counts: int) -> dict[str, Any]:
    return {"name": "db", "passed": ok, "counts": counts}


def test_build_result_totals_count_integration_apart_from_unit() -> None:
    tasks = [
        {"id": "a", "integration": [_row(True, passed=4)]},
        {"id": "b", "integration": [_row(True, passed=1), _row(False, passed=2, failed=1, error=3)]},
        {"id": "c", "check_scope": {"tests_run": 9}},
    ]
    phases = [
        {"id": "p1", "status": "complete", "level_integration": [_row(True, passed=1)]},
        {"id": "p2", "status": "partial", "level_integration": [_row(False, failed=1)]},
        {"id": "p3", "status": "complete"},
    ]
    result = epic._build_result(
        run_id="r5",
        date="2026-11-07",
        initiative_id="i",
        phases=phases,
        tasks=tasks,
        quarantined=[],
        proposals=[],
        stacks_rebased=0,
        paused_until=None,
        repo="/nonexistent",
    )
    assert {k: v for k, v in result["totals"].items() if k.startswith("integration_")} == {
        "integration_tasks_passed": 1,
        "integration_tasks_failed": 1,
        "integration_levels_passed": 1,
        "integration_levels_failed": 1,
        "integration_rows_passed": 7,
        "integration_rows_failed": 1,
        "integration_rows_error": 3,
    }
