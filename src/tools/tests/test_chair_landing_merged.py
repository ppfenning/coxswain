"""The merged flag has one meaning at `initiative`: the task's phase is merged into the initiative branch, per the landing record."""
from agent_tools.chair_facts import running_initiatives, with_landing_merged


def item(ident: str, phase: str, state: str, *, initiative: str = "a", merged: bool = False, needs=(), surfaces=()) -> dict:
    return {"initiative": initiative, "id": ident, "phase": phase, "state": state, "merged": merged, "needs": list(needs),
            "surfaces": list(surfaces)}


def record(initiative: str = "a", **phases: dict) -> dict:
    return {"initiative": initiative, "branch": f"init/{initiative}", "parent": "main", "state": "building", "phases": phases}


ITEMS = [
    item("a1", "p1", "approved", merged=True, surfaces=["src/one.py"]),
    item("a2", "p2", "approved", merged=True, needs=["a1"], surfaces=["src/two.py"]),
    item("a3", "p3", "ready", needs=["a2"], surfaces=["src/three.py"]),
]


def flags(items: list[dict]) -> dict[str, bool]:
    return {i["id"]: bool(i.get("merged")) for i in items}


def test_an_approved_task_is_merged_exactly_when_the_record_says_its_phase_merged() -> None:
    """The flag each reader found (on the phase branch, in the old meaning) is replaced, not combined."""
    one = with_landing_merged(ITEMS, [record(p1={"state": "merged", "tasks": ["a1"]})], {"a": "initiative"})
    assert flags(one) == {"a1": True, "a2": False, "a3": False}
    both = with_landing_merged(ITEMS, [record(p1={"state": "merged"}, p2={"state": "merged"})], {"a": "ticket"})
    assert flags(both) == {"a1": True, "a2": True, "a3": False}


def test_an_initiative_with_no_record_has_nothing_merged() -> None:
    assert flags(with_landing_merged(ITEMS, [], {"a": "initiative"})) == {"a1": False, "a2": False, "a3": False}
    assert flags(with_landing_merged(ITEMS, [record("other", p1={"state": "merged"})], {})) == {"a1": False, "a2": False, "a3": False}


def test_a_task_initiative_keeps_the_flag_its_reader_found() -> None:
    assert with_landing_merged(ITEMS, [], {"a": "task"}) == ITEMS


def test_a_merged_phase_that_gained_work_is_not_merged_until_it_merges_again() -> None:
    items = [item("a1", "p1", "approved", merged=True), item("a1-fix", "p1", "approved", merged=True)]
    carried = [record(p1={"state": "merged", "tasks": ["a1"]})]
    assert flags(with_landing_merged(items, carried, {"a": "initiative"})) == {"a1": False, "a1-fix": False}


def test_steer_holds_no_files_for_a_task_whose_phase_is_merged_and_holds_them_for_one_that_is_not() -> None:
    """A built phase frees its files minutes after it is built, when the lander merges it: not when the pull request lands."""
    seen = with_landing_merged(ITEMS, [record(p1={"state": "merged", "tasks": ["a1"]})], {"a": "initiative"})
    (running,) = running_initiatives(set(), seen, {"a": "r"}, unlanded={"a"})
    assert running["surfaces"] == ["src/two.py"], "p1 is merged and holds nothing; p2 is built, unmerged, and still holds its file"
    all_merged = with_landing_merged(ITEMS, [record(p1={"state": "merged"}, p2={"state": "merged"})], {"a": "initiative"})
    assert [r["surfaces"] for r in running_initiatives(set(), all_merged, {"a": "r"}, unlanded={"a"})] in ([[]], [])
