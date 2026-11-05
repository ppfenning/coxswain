import copy

import pytest

from harness.surface_heal import heal_new_markers, malformed_surfaces

PATHS = frozenset({"src/a.py", "src/pkg/b.py", "docs/guide.md"})


def test_two_missing_files_get_the_marker_and_two_notes_in_order():
    task = {"id": "t", "surfaces": ["src/new_one.py", "src/a.py", "docs/new_two.md"], "lint": ["earlier note"]}
    out = heal_new_markers(task, PATHS)
    assert out["surfaces"] == ["src/new_one.py (new)", "src/a.py", "docs/new_two.md (new)"]
    assert out["lint"] == [
        "earlier note",
        "src/new_one.py: missing from the repository, healed by the surface check",
        "docs/new_two.md: missing from the repository, healed by the surface check",
    ]
    assert out["id"] == "t"


def test_an_existing_file_is_unchanged_and_adds_no_lint_key():
    task = {"surfaces": ["src/a.py"]}
    assert heal_new_markers(task, PATHS) == task
    assert "lint" not in heal_new_markers(task, PATHS)


def test_a_directory_with_files_under_it_is_unchanged():
    task = {"surfaces": ["src/pkg", "src/pkg/"]}
    assert heal_new_markers(task, PATHS) == task


def test_an_already_marked_entry_gets_no_second_marker_and_no_note():
    task = {"surfaces": ["src/fresh.py (new)"], "lint": ["kept"]}
    out = heal_new_markers(task, PATHS)
    assert out == task
    assert out["lint"] == ["kept"]


@pytest.mark.parametrize("entry", ["/abs/x.py", "../x.py", "src/../x.py", "src/*.py", "src/a?.py", "src/[ab].py"])
def test_each_malformed_shape_is_reported_and_left_unmarked(entry):
    assert malformed_surfaces(["src/a.py", entry]) == [entry]
    assert malformed_surfaces([f"{entry} (new)"]) == [f"{entry} (new)"]
    task = {"surfaces": [entry]}
    assert heal_new_markers(task, PATHS) == task


def test_a_dotted_name_is_not_a_parent_segment():
    assert malformed_surfaces(["src/a..b.py", "src/a.py (new)"]) == []


def test_the_input_task_is_not_mutated():
    task = {"surfaces": ["src/new.py", "src/a.py"], "lint": ["earlier"]}
    before = copy.deepcopy(task)
    out = heal_new_markers(task, PATHS)
    assert task == before
    assert out["surfaces"] is not task["surfaces"]
    assert out["lint"] is not task["lint"]
