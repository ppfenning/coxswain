import pytest
from test_chair_report import NOW, _facts

from agent_tools.chair_report import format_status


def _line(**extra):
    return format_status({**_facts(), **extra}, [], [], NOW).split(" | ")


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
