"""A child phase plans no land until the parent phase holding a task it needs has landed."""
from agent_tools.chair_plan_land import parent_waits, plan_lands


def _task(id: str, phase: str, needs: list[str], done: bool = True) -> dict:
    return {
        "id": id, "initiative": "i", "repo": "r", "phase": phase, "phase_done": done, "needs": needs, "run": "i-1",
        "needs_fetch": False,
    }


def _facts(approved: list[dict], landed: set[str], rows: list[tuple[str, list[tuple[str, str]]]], land_at: str = "task") -> dict:
    return {
        "approved": approved,
        "initiatives": [{"id": "i", "started": True, "ready_tasks": [], "landed": landed, "land_at": land_at}],
        "phase_tasks": [{"initiative": "i", "phase": phase, "tasks": tasks} for phase, tasks in rows],
    }


def _lands(facts: dict) -> set[str]:
    return {a["phase"] for a in plan_lands(facts) if a["kind"] == "land_phase"}


def _both_approved(landed: set[str], land_at: str = "task") -> dict:
    return _facts(
        [_task("a", "p1", []), _task("b", "p2", ["a"])], landed,
        [("p1", [("a", "approved")]), ("p2", [("b", "approved")])], land_at,
    )


def test_the_parent_is_planned_and_the_child_is_held_in_the_same_tick():
    assert _lands(_both_approved(set())) == {"p1"}


def test_the_child_is_held_with_the_parent_wait_text():
    assert parent_waits(_both_approved(set())) == {("i", "p2"): "waiting on parent land: p1"}


# The landed pair shares one shape: task a is in phase_tasks under p1 but not in approved, so only the landed set differs.
def _landed_parent(landed: set[str]) -> dict:
    return _facts([_task("b", "p2", ["a"])], landed, [("p1", [("a", "approved")]), ("p2", [("b", "approved")])])


def test_the_child_is_planned_once_the_parent_ids_are_in_the_landed_set():
    assert _lands(_landed_parent({"a"})) == {"p2"}


def test_the_child_is_held_when_the_parent_is_mapped_but_not_landed():
    assert _lands(_landed_parent(set())) == set()


def test_nothing_is_planned_for_the_child_while_the_parent_is_incomplete():
    facts = _facts(
        [_task("a", "p1", [], done=False), _task("b", "p2", ["a"])], set(),
        [("p1", [("a", "approved"), ("c", "ready")]), ("p2", [("b", "approved")])],
    )
    assert _lands(facts) == set()


def test_a_phase_with_no_cross_phase_need_is_unaffected():
    facts = _facts(
        [_task("a", "p1", []), _task("b", "p2", [])], set(),
        [("p1", [("a", "approved")]), ("p2", [("b", "approved")])],
    )
    assert _lands(facts) == {"p1", "p2"}


def test_an_initiative_land_at_is_not_held_by_this_rule():
    assert parent_waits(_both_approved(set(), land_at="initiative")) == {}


def test_a_held_child_with_a_quarantined_sibling_plans_no_partial_land():
    facts = _facts(
        [_task("a", "p1", []), _task("b", "p2", ["a"])], set(),
        [("p1", [("a", "approved")]), ("p2", [("b", "approved"), ("q", "quarantined")])],
    )
    assert [a for a in plan_lands(facts) if a["kind"] == "partial_land" and a["phase"] == "p2"] == []
