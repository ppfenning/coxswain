from agent_tools import route
from agent_tools.chair_read_docket import _launchable, docket_from_builder


def _item(id: str, phase: str, state: str, needs: tuple[str, ...] = (), initiative: str = "a", merged: bool = False) -> dict:
    return {
        "id": id, "initiative": initiative, "phase": phase, "state": state, "needs": list(needs),
        "file": f"{phase}/{id}.md", "merged": merged,
    }


def _row(items: list[dict]) -> dict:
    docket = docket_from_builder(route.initiative_summaries(items), items, 0, 4)
    return docket["initiatives"][0]


def test_p2_is_ready_and_launchable_when_its_p1_needs_are_approved_and_merged():
    items = [
        _item("x", "p1", "approved", merged=True),
        _item("y", "p1", "approved", merged=True),
        _item("z", "p2", "ready", needs=("x", "y")),
    ]
    assert route.initiative_summaries(items) == [{"id": "a", "phase": "p2", "ready": 1}]
    row = _row(items)
    assert [t["id"] for t in row["ready_tasks"]] == ["z"]
    assert row["waiting_tasks"] == []
    assert [i["id"] for i in _launchable(row, items)] == ["z"]


def test_p2_waits_when_one_p1_need_is_approved_and_unmerged():
    items = [
        _item("x", "p1", "approved", merged=True),
        _item("y", "p1", "approved"),
        _item("z", "p2", "ready", needs=("x", "y")),
    ]
    assert route.initiative_summaries(items) == [{"id": "a", "phase": None, "ready": 0, "awaiting_merge": 1}]
    row = _row(items)
    assert row["ready_tasks"] == []
    assert [t["id"] for t in row["waiting_tasks"]] == ["z"]


def test_a_same_phase_merged_need_does_not_hold_its_dependent():
    items = [_item("x", "p1", "approved", merged=True), _item("z", "p1", "ready", needs=("x",))]
    row = _row(items)
    assert [t["id"] for t in row["ready_tasks"]] == ["z"]
    assert row["held_tasks"] == []


def test_a_same_phase_unmerged_need_still_holds_its_dependent():
    items = [_item("x", "p1", "approved"), _item("z", "p1", "ready", needs=("x",))]
    row = _row(items)
    assert row["ready_tasks"] == []
    assert [t["id"] for t in row["held_tasks"]] == ["z"]


def test_a_merged_need_in_another_initiative_does_not_satisfy_a_need():
    items = [
        _item("x", "p1", "approved", initiative="b", merged=True),
        _item("z", "p1", "ready", needs=("x",)),
    ]
    row = next(r for r in docket_from_builder(route.initiative_summaries(items), items, 0, 4)["initiatives"] if r["id"] == "a")
    assert row["ready_tasks"] == []
    assert [t["id"] for t in row["waiting_tasks"]] == ["z"]
