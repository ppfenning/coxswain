from __future__ import annotations

import harness.cli as cli


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
