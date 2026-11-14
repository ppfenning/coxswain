import ast
from pathlib import Path

from agent_tools import chair_facts, chair_sort
from agent_tools.chair_sort import (
    HARNESS_CAUSE,
    MAX_NOTE,
    MAX_OBJECTION_IN_NOTE,
    NOTE_HEADING,
    OBJECTION_CAUSE,
    ObjectionNote,
    classify,
    objection_key,
    rebuild_note,
    state_key,
)
from agent_tools.chair_types import QuarantineFacts

_ALLOWED_IMPORTS = {"hashlib", "re", "collections.abc", "typing", "agent_tools.chair_types"}


def _q(cause: str, reason: str | None = None, causes: list[str] | None = None) -> QuarantineFacts:
    q: dict = {
        "task_id": "t1",
        "initiative": "i",
        "cause": cause,
        "harness_failures": 1,
        "has_patch": False,
        "rescue_failed": False,
    }
    if reason is not None:
        q["reason"] = reason
    if causes is not None:
        q["causes"] = causes
    return q  # type: ignore[return-value]


def test_the_harness_cause_is_the_one_the_producers_emit() -> None:
    assert HARNESS_CAUSE == chair_facts.HARNESS_CAUSE


def test_a_harness_quarantine_rebuilds_on_main_the_first_time() -> None:
    assert classify(_q("harness", "patch did not apply: x", causes=["harness"]), "s1", set()) == "rebuild_on_main"


def test_a_repeated_harness_state_is_skipped() -> None:
    assert classify(_q("harness"), "s1", {state_key("t1", "s1")}) == "skip"


def test_a_changed_task_state_rebuilds_again() -> None:
    assert classify(_q("harness"), "s2", {state_key("t1", "s1")}) == "rebuild_on_main"


def test_the_same_state_on_another_task_rebuilds() -> None:
    assert classify(_q("harness"), "s1", {state_key("t2", "s1")}) == "rebuild_on_main"


def test_a_first_held_objection_is_annotated() -> None:
    assert classify(_q(OBJECTION_CAUSE, "Missing a test for the empty row"), "s1", set()) == "annotate_objection"


def test_a_repeated_normalised_objection_needs_attention() -> None:
    handled = {objection_key("t1", "missing a test for the empty row")}
    assert classify(_q("review", "Missing  a TEST\nfor the empty row "), "s2", handled) == "needs_attention"


def test_a_different_objection_is_annotated_not_attention() -> None:
    handled = {objection_key("t1", "missing a test for the empty row")}
    assert classify(_q("review", "The retry loop never ends"), "s2", handled) == "annotate_objection"


def test_the_same_objection_on_another_task_is_annotated() -> None:
    handled = {objection_key("t2", "missing a test")}
    assert classify(_q("review", "Missing a test"), "s1", handled) == "annotate_objection"


def test_an_absent_objection_needs_attention() -> None:
    assert classify(_q("review"), "s1", set()) == "needs_attention"


def test_a_blank_objection_needs_attention_even_unhandled() -> None:
    assert classify(_q("review", " \n\t "), "s1", set()) == "needs_attention"


def test_ticket_code_runaway_and_unknown_causes_need_attention() -> None:
    assert [classify(_q(c), "s1", set()) for c in ("ticket", "code", "runaway", "stranded", "main_red", "")] == [
        "needs_attention"
    ] * 6


def test_objection_key_ignores_case_and_whitespace() -> None:
    assert objection_key("t1", "Fix  the\tparser") == objection_key("t1", " fix the parser\n")


def test_objection_key_differs_by_text_and_task() -> None:
    assert objection_key("t1", "fix the parser") != objection_key("t1", "fix the writer")
    assert objection_key("t1", "fix the parser") != objection_key("t2", "fix the parser")


def test_rebuild_note_is_headed_and_carries_the_instruction() -> None:
    note = rebuild_note("Missing a test", "Add a test for the empty row.")
    assert note == f"{NOTE_HEADING}\n\nObjection: Missing a test\n\nInstruction: Add a test for the empty row."


def test_a_long_instruction_is_cut_at_a_word_and_the_heading_survives() -> None:
    note = rebuild_note("Missing a test", "word " * 500)
    assert note.startswith(NOTE_HEADING)
    assert len(note) <= MAX_NOTE
    assert note.endswith("word…")


def test_a_long_objection_is_cut_and_leaves_room_for_the_instruction() -> None:
    note = rebuild_note("o " * 500, "Add the test.")
    objection_line = note.split("\n\n")[1]
    assert len(objection_line.removeprefix("Objection: ")) <= MAX_OBJECTION_IN_NOTE
    assert note.endswith("Instruction: Add the test.")


def test_the_injected_step_feeds_rebuild_note() -> None:
    step: ObjectionNote = lambda objection: f"Address: {objection}"  # noqa: E731
    assert rebuild_note("Missing a test", step("Missing a test")).endswith("Instruction: Address: Missing a test")


def test_the_module_imports_only_pure_modules() -> None:
    tree = ast.parse(Path(chair_sort.__file__).read_text())
    imported = {
        name
        for node in ast.walk(tree)
        for name in (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    }
    assert imported == _ALLOWED_IMPORTS
