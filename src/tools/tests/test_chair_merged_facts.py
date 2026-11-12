from agent_tools.chair_facts import approved_facts, initiative_facts, land_at_facts
from agent_tools.chair_read_approved import approved_rows, mark_landed, phase_task_rows


def _item(id: str, state: str, merged: bool = False) -> dict:
    return {"id": id, "initiative": "i", "phase": "p", "state": state, "repo": "r", "needs": [], **({"merged": True} if merged else {})}


def test_merged_approved_item_reads_merged_with_flag() -> None:
    items = [_item("a", "approved", merged=True), _item("b", "ready")]
    assert phase_task_rows(items)[0]["tasks"] == [("a", "merged"), ("b", "ready")]
    assert approved_rows(items)[0]["merged"] is True


def test_unmerged_approved_item_keeps_approved() -> None:
    items = [_item("a", "approved")]
    assert phase_task_rows(items)[0]["tasks"] == [("a", "approved")]
    assert approved_rows(items)[0]["merged"] is False


def test_landed_record_reads_done_even_when_merged() -> None:
    items = mark_landed([_item("a", "approved", merged=True), _item("b", "approved", merged=True)], {"a"})
    assert phase_task_rows(items)[0]["tasks"] == [("a", "done"), ("b", "merged")]
    assert [r["id"] for r in approved_rows(items)] == ["b"]


def test_approved_facts_carries_merged_and_defaults_false() -> None:
    row = {"id": "a", "initiative": "i", "repo": "r", "phase": "p", "phase_done": True, "needs": [], "run": "", "needs_fetch": False}
    assert [r["merged"] for r in approved_facts([row, {**row, "merged": True}])] == [False, True]


def test_land_at_comes_from_the_cartridge_and_initiative_value() -> None:
    cartridge = {"landing_areas": {"land_at": "level"}}
    # The retired level reads as initiative.
    assert land_at_facts(cartridge, {"b": "task"}, ["a", "b"]) == {"a": "initiative", "b": "task"}


def test_land_at_defaults_to_initiative_without_a_cartridge_or_on_a_bad_value() -> None:
    assert land_at_facts({}, {}, ["a"]) == {"a": "initiative"}
    assert land_at_facts({"landing_areas": {"land_at": "sometimes"}}, {}, ["a"]) == {"a": "initiative"}


def test_initiative_facts_carries_land_at_only_when_given() -> None:
    docket = {"initiatives": [{"id": "a", "started": False, "ready_tasks": [], "landed": []}]}
    assert initiative_facts(docket, set(), land_at={"a": "task"})[0]["land_at"] == "task"
    assert "land_at" not in initiative_facts(docket, set())[0]
