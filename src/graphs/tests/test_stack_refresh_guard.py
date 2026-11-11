"""The rebase guard names recorded merged commits a rebase lost. Plain data, no repository."""

from __future__ import annotations

from harness.stack_refresh import lost_merges, rebase_lost_reason


def test_recorded_unreachable_task_is_lost() -> None:
    assert lost_merges({"t1": "abc"}, {"t1": False}) == ["t1"]


def test_reachable_task_is_kept() -> None:
    assert lost_merges({"t1": "abc"}, {"t1": True}) == []


def test_task_with_no_recorded_commit_is_never_lost() -> None:
    assert lost_merges({"t1": None, "t2": ""}, {"t1": False, "t2": False}) == []


def test_task_missing_from_reachable_is_lost() -> None:
    assert lost_merges({"t1": "abc"}, {}) == ["t1"]


def test_lost_merges_are_sorted() -> None:
    assert lost_merges({"t3": "c", "t1": "a", "t2": "b"}, {}) == ["t1", "t2", "t3"]


def test_reason_names_branch_onto_and_every_lost_task_sorted() -> None:
    assert rebase_lost_reason("phase/p1", "origin/main", ["t2", "t1"]) == (
        "rebasing phase/p1 onto origin/main lost the merged commits of: t1, t2; the branch was left as it was"
    )
