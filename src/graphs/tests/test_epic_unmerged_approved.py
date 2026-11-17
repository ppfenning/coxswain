"""The rule that keeps a phase from merging into the initiative branch: an approved task the store has not recorded as merged."""

from harness.epic import unmerged_approved


def test_an_approved_task_without_a_recorded_merge_keeps_the_phase_back_and_nothing_else_does():
    rows = [
        {"id": "t1", "state": "approved", "merged": False},
        {"id": "t2", "state": "approved", "merged": True},
        {"id": "t3", "state": "done", "merged": False},
        {"id": "t4", "state": "ready"},
        {"id": "t5", "state": "approved"},
    ]
    assert unmerged_approved(rows) == ["t1", "t5"]


def test_no_rows_keep_nothing_back():
    assert unmerged_approved([]) == []
