import copy

import pytest

from harness.surface_heal import heal_bare_filenames, heal_new_markers, malformed_surfaces, notes_out_of_surfaces

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


def test_a_bare_name_with_one_tree_match_becomes_its_repository_path():
    out = heal_bare_filenames({"surfaces": ["a.py", "docs/guide.md"]}, PATHS)
    assert out["surfaces"] == ["src/a.py", "docs/guide.md"]
    assert "lint" not in out


def test_a_marked_bare_name_with_a_body_named_full_path_keeps_the_marker():
    task = {"surfaces": ["new_mod.py (new)"], "body": "Add `src/pkg/new_mod.py`, then wire it."}
    assert heal_bare_filenames(task, PATHS)["surfaces"] == ["src/pkg/new_mod.py (new)"]


def test_an_unresolvable_bare_name_is_left_out_with_a_note_after_existing_lint():
    task = {"surfaces": ["ghost.py", "src/a.py"], "lint": ["earlier"]}
    out = heal_bare_filenames(task, PATHS)
    assert out["surfaces"] == ["src/a.py"]
    assert out["lint"] == ["earlier", "ghost.py: no unique repository path, left out of surfaces"]


def test_an_ambiguous_bare_name_is_left_out():
    paths = PATHS | {"src/x/util.py", "src/y/util.py"}
    out = heal_bare_filenames({"surfaces": ["util.py"], "body": "touch util.py"}, paths)
    assert out["surfaces"] == []
    assert out["lint"] == ["util.py: no unique repository path, left out of surfaces"]


def test_a_body_named_path_settles_a_name_the_tree_cannot():
    paths = PATHS | {"src/x/util.py", "src/y/util.py"}
    task = {"surfaces": ["util.py"], "body": "touch src/x/util.py"}
    assert heal_bare_filenames(task, paths)["surfaces"] == ["src/x/util.py"]


def test_a_dot_directory_in_the_body_keeps_its_leading_dot():
    task = {"surfaces": ["release.yml (new)"], "body": "Edit (.github/workflows/release.yml)."}
    assert heal_bare_filenames(task, PATHS)["surfaces"] == [".github/workflows/release.yml (new)"]
    task = {"surfaces": ["cartridge.yaml"], "body": "See `.agent/cartridge.yaml`, then stop."}
    assert heal_bare_filenames(task, PATHS | {".agent/cartridge.yaml"})["surfaces"] == [".agent/cartridge.yaml"]


def test_a_marked_bare_name_with_one_tree_match_loses_the_marker():
    assert heal_bare_filenames({"surfaces": ["a.py (new)"]}, PATHS)["surfaces"] == ["src/a.py"]


def test_an_unmarked_bare_name_resolved_through_the_body_gets_the_marker_when_absent_from_the_tree():
    task = {"surfaces": ["fresh.py"], "body": "Create src/pkg/fresh.py."}
    assert heal_bare_filenames(task, PATHS)["surfaces"] == ["src/pkg/fresh.py (new)"]


def test_a_tree_match_wins_over_a_body_named_path():
    task = {"surfaces": ["a.py"], "body": "Edit src/other/a.py"}
    assert heal_bare_filenames(task, PATHS)["surfaces"] == ["src/a.py"]


def test_a_body_named_path_in_the_tree_takes_no_marker():
    paths = PATHS | {"src/x/util.py", "src/y/util.py"}
    task = {"surfaces": ["util.py (new)"], "body": "Edit src/y/util.py"}
    assert heal_bare_filenames(task, paths)["surfaces"] == ["src/y/util.py"]


def test_resolved_duplicates_and_repeated_notes_collapse_to_one():
    task = {"surfaces": ["a.py", "src/a.py", "ghost.py", "ghost.py (new)"]}
    out = heal_bare_filenames(task, PATHS)
    assert out["surfaces"] == ["src/a.py"]
    assert out["lint"] == ["ghost.py: no unique repository path, left out of surfaces"]


def test_notes_are_not_bare_filenames_and_the_two_rules_compose_in_either_order():
    note = "edge_unsupported: no handler for x"
    task = {"surfaces": ["a.py", note, "two words"], "lint": ["earlier"]}
    first = notes_out_of_surfaces(heal_bare_filenames(task, PATHS))
    second = heal_bare_filenames(notes_out_of_surfaces(task), PATHS)
    assert first == second
    assert first["surfaces"] == ["src/a.py"]
    assert first["lint"] == ["earlier", note, "two words"]


def test_two_body_named_paths_are_ambiguous():
    task = {"surfaces": ["m.py (new)"], "body": "src/p/m.py and src/q/m.py"}
    assert heal_bare_filenames(task, PATHS)["surfaces"] == []


def test_a_path_with_a_slash_is_untouched_by_the_bare_rule():
    task = {"surfaces": ["src/a.py", "src/new.py (new)"]}
    out = heal_bare_filenames(task, PATHS)
    assert out == task
    assert "lint" not in out


def test_bare_rule_does_not_mutate_its_input():
    task = {"surfaces": ["a.py", "ghost.py"], "lint": ["earlier"], "body": "x"}
    before = copy.deepcopy(task)
    out = heal_bare_filenames(task, PATHS)
    assert task == before
    assert out["surfaces"] is not task["surfaces"]
    assert out["lint"] is not task["lint"]


def test_an_edge_unsupported_note_moves_to_lint_after_existing_entries():
    note = "edge_unsupported: no handler for x"
    task = {"surfaces": ["src/a.py", note, note, "two words (new)"], "lint": ["earlier"]}
    out = notes_out_of_surfaces(task)
    assert out["surfaces"] == ["src/a.py"]
    assert out["lint"] == ["earlier", note, "two words (new)"]


def test_a_note_already_in_lint_is_not_repeated():
    out = notes_out_of_surfaces({"surfaces": ["see: elsewhere"], "lint": ["see: elsewhere"]})
    assert out == {"surfaces": [], "lint": ["see: elsewhere"]}


def test_a_task_with_no_note_comes_back_equal_without_a_lint_key():
    task = {"surfaces": ["src/a.py", "src/new.py (new)", "key:value.py"]}
    out = notes_out_of_surfaces(task)
    assert out == task
    assert "lint" not in out


def test_note_rule_does_not_mutate_its_input():
    task = {"surfaces": ["src/a.py", "a note"], "lint": ["earlier"]}
    before = copy.deepcopy(task)
    out = notes_out_of_surfaces(task)
    assert task == before
    assert out["lint"] is not task["lint"]
