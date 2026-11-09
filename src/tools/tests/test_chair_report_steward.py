import pytest
from test_chair_report import NOW, _facts

from agent_tools.chair_report import format_status


def _line(results=(), **extra):
    return format_status({**_facts(), **extra}, [], list(results), NOW).split(" | ")


def _merge(status, kind="merge_level"):
    return {"action": {"kind": kind}, "status": status, "reason": "repo lock held" if status == "deferred" else ""}


def _managed(n):
    return {"enabled": True, "epics": [{"epic_id": f"init-{i}"} for i in range(n)]}


@pytest.mark.parametrize("n", [3, 0])
def test_enabled_steward_prints_its_managed_count(n):
    assert f"steward {n}" in _line(steward=_managed(n))


def test_steward_sits_before_the_skipped_repo_parts():
    parts = _line(steward=_managed(3), missing_repos=["/repo/a"])
    assert parts.index("steward 3") + 1 == parts.index("skipped missing repo /repo/a")


@pytest.mark.parametrize("extra", [{"steward": {"enabled": False, "epics": [{"epic_id": "a"}]}}, {}])
def test_disabled_or_absent_steward_prints_nothing(extra):
    assert not [p for p in _line(**extra) if p.startswith("steward")]


def test_done_and_deferred_merges_are_counted_separately():
    assert "steward 2 (merged 1, deferred 1)" in _line([_merge("done"), _merge("deferred")], steward=_managed(2))


def test_deferred_merge_adds_nothing_to_the_failed_part():
    assert not [p for p in _line([_merge("deferred")], steward=_managed(1)) if p.startswith("failed")]


@pytest.mark.parametrize("results", [[], [_merge("done", kind="launch")], [_merge("failed")]])
def test_no_counted_merge_prints_the_bare_count(results):
    assert "steward 2" in _line(results, steward=_managed(2))
