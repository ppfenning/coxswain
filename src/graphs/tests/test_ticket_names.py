from harness.ticket_names import NameFindings, check_names

PATHS = frozenset({"src/cli.py", "src/xcli.py", "src/util.py", "src/pkg/util.py", "src/pkg/b.py", "src/a.py"})
DEFINED = {"real_fn": ["src/a.py"]}


def defined(name: str) -> list[str]:
    return DEFINED.get(name, [])


def test_a_bare_filename_with_one_match_is_replaced_and_noted():
    task = {"id": "t", "surfaces": ["cli.py"], "lint": ["earlier"], "body": "Work happens in one file."}
    out = check_names(task, PATHS, defined)
    assert out.task["surfaces"] == ["src/cli.py"]
    assert out.task["lint"] == ["earlier", "cli.py: resolved to src/cli.py, healed by the ticket check"]
    assert out.task["id"] == "t"
    assert out.corrections == ()


def test_an_ambiguous_bare_filename_stays_and_lists_the_candidates():
    task = {"surfaces": ["util.py"], "body": ""}
    out = check_names(task, PATHS, defined)
    assert out.task == task
    assert out.corrections == (
        "util.py matches several files: src/pkg/util.py, src/util.py. The surface stays bare until one is named.",
    )


def test_a_bare_filename_with_no_match_stays_and_is_reported_missing():
    task = {"surfaces": ["gone.py"]}
    out = check_names(task, PATHS, defined)
    assert out.task == task
    assert out.corrections == ("gone.py was not found in the repository. No file path ends in that name.",)


def test_a_body_named_file_missing_from_the_repository_is_reported_unless_a_surface_marks_it():
    task = {"surfaces": ["src/a.py", "src/fresh.py (new)"], "body": "Read src/ghost.py, then create src/fresh.py."}
    out = check_names(task, PATHS, defined)
    assert out.task == task
    assert out.corrections == (
        "src/ghost.py is not in the repository and the surface list does not mark it (new). "
        "Mark it (new) if the task creates it.",
    )


def test_a_surface_outside_the_repository_is_reported_and_never_healed():
    task = {"surfaces": ["../x.py", "/abs/y.py"]}
    out = check_names(task, PATHS, defined)
    assert out.task == task
    assert out.corrections == (
        "../x.py lies outside the repository. It is reported here and not healed.",
        "/abs/y.py lies outside the repository. It is reported here and not healed.",
    )


def test_a_file_the_body_says_will_change_is_appended_with_a_note_and_a_read_only_file_is_not():
    task = {
        "surfaces": ["src/a.py"],
        "lint": ["earlier"],
        "body": "Read src/cli.py for the pattern. Then update src/pkg/b.py to match.",
    }
    out = check_names(task, PATHS, defined)
    assert out.task["surfaces"] == ["src/a.py", "src/pkg/b.py"]
    assert out.task["lint"] == [
        "earlier",
        "added to surfaces: src/pkg/b.py was named in the body and healed by the ticket check",
    ]
    assert out.corrections == ()


def test_a_nonexistent_function_is_reported_and_one_the_task_creates_is_not():
    body = "Call `ghost()` from the loop. Also add `add_ghost()` as a new helper. The `real_fn()` stays."
    out = check_names({"surfaces": ["src/a.py"], "body": body}, PATHS, defined)
    assert out.corrections == ("ghost was not found in the tree. No def or class of that name exists.",)


def test_a_name_next_to_the_word_class_is_checked():
    out = check_names({"body": "The class `Phantom` owns this."}, PATHS, defined)
    assert out.corrections == ("Phantom was not found in the tree. No def or class of that name exists.",)


def test_a_ticket_whose_names_all_resolve_comes_back_unchanged():
    task = {
        "surfaces": ["src/a.py", "src/pkg"],
        "lint": ["kept"],
        "body": "Update src/a.py and call `real_fn()`. Read src/pkg/b.py first.",
    }
    out = check_names(task, PATHS, defined)
    assert out == NameFindings(task=task, corrections=())
