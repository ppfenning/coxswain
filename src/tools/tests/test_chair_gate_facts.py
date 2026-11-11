from dataclasses import replace

from agent_tools.chair_exec import argv_for
from agent_tools.chair_facts import gather_facts, intake_paths_from_rows
from agent_tools.chair_read_intake import intake_from_rows
from tests.test_chair_facts import NOW, _deps


def intake(task_id: str, state: str = "queued", **extra: object) -> dict:
    return {"kind": "intake", "initiative": "intake", "task_id": task_id, "state": state, "extra": dict(extra)}


def initiative(name: str, **extra: object) -> dict:
    return {"kind": "initiative", "initiative": name, "task_id": name, "state": "todo", "extra": dict(extra)}


def _gathered(rows: list[dict], current: str = "2026.11.5") -> dict:
    return gather_facts(replace(_deps(), queue=lambda: rows, current_version=lambda: current), NOW)


def test_a_queued_intake_held_with_a_quoted_value_yields_no_path() -> None:
    assert intake_paths_from_rows([intake("a", held="true"), intake("b")]) == ["intake/b.md"]


def test_a_bare_keyed_queued_intake_launches_decompose_on_the_file_under_intake() -> None:
    [idea] = intake_paths_from_rows([intake("2026-11-11-some-idea")])
    argv = argv_for({"kind": "launch_decompose", "intake_ids": [idea]}, "some-idea")
    assert argv is not None and argv[argv.index("--idea") + 1] == "intake/2026-11-11-some-idea.md"


def test_a_queued_intake_with_a_recorded_path_keeps_it() -> None:
    rows = [intake("a", path="intake/elsewhere.md"), intake("b", path="/abs/ws/intake/b.md")]
    assert intake_paths_from_rows(rows) == ["intake/elsewhere.md", "/abs/ws/intake/b.md"]


def test_a_queued_intake_with_a_later_target_release_yields_no_path() -> None:
    rows = [intake("a", target_release="2026.12.0")]
    assert intake_paths_from_rows(rows, "2026.11.5") == []


def test_a_current_version_at_or_above_the_target_yields_the_path() -> None:
    rows = [intake("a", target_release="2026.12.0")]
    assert intake_paths_from_rows(rows, "2026.12.0") == ["intake/a.md"]
    assert intake_paths_from_rows(rows, "2027.1.0") == ["intake/a.md"]


def test_intake_from_rows_without_current_ignores_target_release() -> None:
    rows = [intake("a", target_release="2026.12.0")]
    assert [r["task_id"] for r in intake_from_rows(rows)] == ["a"]


def test_the_gathered_intake_fact_drops_held_and_deferred_intakes() -> None:
    rows = [intake("held", held="'true'"), intake("later", target_release="2026.12.0"), intake("free")]
    assert _gathered(rows)["intake"] == ["intake/free.md"]


def test_a_held_decomposed_intake_marks_its_initiative() -> None:
    rows = [intake("a", "decomposed", initiative="alpha", held="true"), intake("b", "decomposed", initiative="beta")]
    assert _gathered(rows)["held_initiatives"] == ["alpha"]


def test_a_deferred_initiative_row_appears_with_its_target() -> None:
    rows = [initiative("alpha", target_release="2026.12.0"), initiative("beta", target_release="2026.11.0")]
    assert _gathered(rows)["deferred_initiatives"] == {"alpha": "2026.12.0"}


def test_no_rows_gives_empty_gate_facts() -> None:
    facts = gather_facts(_deps(), NOW)
    assert (facts["held_initiatives"], facts["deferred_initiatives"]) == ([], {})
