"""The rerun rule: a failed land check is rerun once unless the change touches a failed suite's folder."""
from __future__ import annotations

from agent_tools.land import rerun_scope


def test_empty_failed_steps_are_eligible() -> None:
    assert rerun_scope([], ["src/tools/agent_tools/land.py"]) is True


def test_failed_suite_the_change_does_not_touch_is_eligible() -> None:
    assert rerun_scope(["pytest tools"], ["src/graphs/a.py"]) is True


def test_failed_suite_the_change_touches_is_not_eligible() -> None:
    assert rerun_scope(["pytest tools"], ["src/graphs/a.py", "src/tools/agent_tools/land.py"]) is False


def test_sibling_folder_with_the_same_prefix_is_not_the_suite() -> None:
    assert rerun_scope(["pytest tools"], ["src/tools2/x.py"]) is True


def test_root_suite_is_touched_by_a_path_outside_src() -> None:
    assert rerun_scope(["pytest root"], ["README.md"]) is False


def test_root_suite_is_not_touched_by_src_paths() -> None:
    assert rerun_scope(["pytest root"], ["src/tools/agent_tools/land.py"]) is True


def test_unattributable_step_is_eligible_whatever_the_change() -> None:
    assert rerun_scope(["lint"], ["src/tools/a.py", "README.md"]) is True


def test_bare_pytest_step_is_unattributable() -> None:
    assert rerun_scope(["pytest"], ["src/tools/a.py"]) is True


def test_unattributable_step_beside_a_touched_suite_is_eligible() -> None:
    assert rerun_scope(["pytest tools", "build"], ["src/tools/a.py"]) is True


def test_any_touched_suite_among_several_is_not_eligible() -> None:
    assert rerun_scope(["pytest graphs", "pytest tools"], ["src/tools/a.py"]) is False
