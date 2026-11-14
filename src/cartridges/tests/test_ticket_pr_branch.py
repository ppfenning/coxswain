from __future__ import annotations

from core.landing import phase_merge_ready, pr_branch_name, pr_stage


def test_pr_branch_name() -> None:
    assert pr_branch_name("alpha") == "pr/alpha"


def test_merge_ready_at_ticket_when_all_settled() -> None:
    assert phase_merge_ready("ticket", ["approved", "done", "dropped"], [False, False, False]) is True


def test_merge_not_ready_at_task_and_the_retired_level_reads_as_initiative() -> None:
    assert phase_merge_ready("task", ["approved"], [False]) is False
    assert phase_merge_ready("level", ["approved"], [False]) is True
    assert phase_merge_ready("initiative", ["approved"], [False]) is True


def test_merge_not_ready_with_an_unsettled_task() -> None:
    assert phase_merge_ready("ticket", ["approved", "open"], [False, False]) is False


def test_merge_not_ready_when_empty() -> None:
    assert phase_merge_ready("ticket", [], []) is False


def test_merge_not_ready_with_a_quarantined_task() -> None:
    assert phase_merge_ready("ticket", ["approved", "done"], [False, True]) is False


def test_pr_stage_first_is_draft() -> None:
    assert pr_stage("a", ["a", "b", "c"]) == "draft"


def test_pr_stage_middle_is_update() -> None:
    assert pr_stage("b", ["a", "b", "c"]) == "update"


def test_pr_stage_last_is_ready() -> None:
    assert pr_stage("c", ["a", "b", "c"]) == "ready"


def test_pr_stage_one_phase_is_ready() -> None:
    assert pr_stage("a", ["a"]) == "ready"
