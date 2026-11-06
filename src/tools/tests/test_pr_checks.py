import dataclasses

import pytest

from agent_tools.pr_checks import verdict


def _row(name: str, state: str) -> dict[str, str]:
    return {"name": name, "state": state}


def test_ready_when_every_required_check_succeeded() -> None:
    v = verdict(("lint", "test"), [_row("lint", "success"), _row("test", "success")])
    assert v.kind == "ready"


def test_red_names_each_failed_check() -> None:
    v = verdict(("lint", "test", "docs"), [_row("lint", "failure"), _row("test", "failure"), _row("docs", "success")])
    assert v.kind == "red"
    assert "lint" in v.reason and "test" in v.reason and "docs" not in v.reason


def test_missing_names_each_absent_check_and_is_not_ready() -> None:
    v = verdict(("lint", "test"), [_row("lint", "success")])
    assert v.kind == "missing"
    assert "test" in v.reason and "lint" not in v.reason


def test_pending_when_all_reported_and_one_pending() -> None:
    v = verdict(("lint", "test"), [_row("lint", "success"), _row("test", "pending")])
    assert v.kind == "pending"
    assert "test" in v.reason


def test_unknown_when_required_unreadable() -> None:
    v = verdict(None, [_row("lint", "success")])
    assert v.kind == "unknown"


def test_none_required_is_its_own_kind() -> None:
    v = verdict((), [_row("lint", "success")])
    assert v.kind == "none_required"


def test_rerun_newest_listed_row_wins() -> None:
    assert verdict(("test",), [_row("test", "failure"), _row("test", "success")]).kind == "ready"
    assert verdict(("test",), [_row("test", "success"), _row("test", "failure")]).kind == "red"


def test_extra_red_check_that_is_not_required_stays_ready() -> None:
    v = verdict(("lint",), [_row("lint", "success"), _row("optional", "failure")])
    assert v.kind == "ready"


def test_failure_beats_missing() -> None:
    assert verdict(("lint", "test"), [_row("lint", "failure")]).kind == "red"


def test_missing_beats_pending() -> None:
    assert verdict(("lint", "test"), [_row("lint", "pending")]).kind == "missing"


def test_skipped_required_check_is_not_ready() -> None:
    v = verdict(("lint",), [_row("lint", "skipped")])
    assert v.kind == "pending"
    assert "lint" in v.reason


def test_verdict_is_frozen() -> None:
    v = verdict(None, [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.kind = "ready"  # type: ignore[misc]
