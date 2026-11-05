from __future__ import annotations

import pytest

from core.landing import need_met


def test_done_is_met() -> None:
    assert need_met("task", "done", False) is True


def test_dropped_is_met() -> None:
    assert need_met("task", "dropped", False) is True


def test_approved_merged_is_met() -> None:
    assert need_met("task", "approved", True) is True


def test_approved_unmerged_at_ticket_is_met() -> None:
    assert need_met("ticket", "approved", False) is True


def test_approved_unmerged_at_level_is_met() -> None:
    assert need_met("level", "approved", False) is True


def test_approved_unmerged_at_task_is_not_met() -> None:
    assert need_met("task", "approved", False) is False


def test_ready_is_not_met() -> None:
    assert need_met("ticket", "ready", True) is False


def test_unknown_land_at_raises() -> None:
    with pytest.raises(ValueError, match="need land_at must be one of"):
        need_met("sprint", "done", True)
