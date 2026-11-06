"""launchable_phases: a phase starts when its parent phases' tasks are all met and one of its tasks is ready."""

from agent_tools.chair_phases import _phase_parents, launchable_phases


def row(id, phase, state, needs=(), merged=False):
    return {"id": id, "phase": phase, "state": state, "merged": merged, "needs": list(needs)}


def test_two_independent_ready_phases_give_two_entries():
    items = [row("a1", "p1", "ready"), row("b1", "p2", "ready"), row("b2", "p2", "ready")]
    assert launchable_phases(items, "task") == {"p1": ["a1"], "p2": ["b1", "b2"]}


def test_phases_keep_first_appearance_order():
    items = [row("z1", "pz", "ready"), row("a1", "pa", "ready")]
    assert list(launchable_phases(items, "task")) == ["pz", "pa"]


def test_child_is_absent_while_any_parent_task_is_unmet():
    items = [
        row("a1", "p1", "approved", merged=True),
        row("a2", "p1", "running"),
        row("b1", "p2", "ready", ["a1"]),
    ]
    assert launchable_phases(items, "task") == {}


def test_child_is_present_once_parent_tasks_are_met_even_with_an_unmet_parent_sibling():
    items = [
        row("a1", "p1", "approved", merged=True),
        row("a2", "p1", "dropped"),
        row("s1", "p3", "running"),
        row("b1", "p2", "ready", ["a1"]),
        row("c1", "p4", "ready", ["s1"]),
    ]
    assert launchable_phases(items, "task") == {"p2": ["b1"]}


def test_child_waits_on_every_task_of_the_parent_phase_not_only_the_needed_one():
    items = [row("a1", "p1", "approved", merged=True), row("a2", "p1", "ready"), row("b1", "p2", "ready", ["a1"])]
    assert launchable_phases(items, "task") == {"p1": ["a2"]}


def test_phase_whose_only_ready_task_has_an_unmet_need_is_omitted():
    items = [row("a1", "p1", "running"), row("a2", "p1", "ready", ["a1"])]
    assert launchable_phases(items, "task") == {}


def test_need_naming_no_row_is_unmet():
    assert launchable_phases([row("a1", "p1", "ready", ["gone"])], "task") == {}


def test_phase_with_no_ready_task_is_omitted():
    assert launchable_phases([row("a1", "p1", "running"), row("a2", "p1", "done")], "task") == {}


def test_land_at_task_treats_an_approved_unmerged_parent_as_unmet():
    items = [row("a1", "p1", "approved"), row("b1", "p2", "ready", ["a1"])]
    assert launchable_phases(items, "task") == {}


def test_land_at_task_meets_an_approved_merged_parent():
    items = [row("a1", "p1", "approved", merged=True), row("b1", "p2", "ready", ["a1"])]
    assert launchable_phases(items, "task") == {"p2": ["b1"]}


def test_land_at_level_meets_an_approved_unmerged_parent():
    items = [row("a1", "p1", "approved"), row("b1", "p2", "ready", ["a1"])]
    assert launchable_phases(items, "level") == {"p2": ["b1"]}


def test_quarantined_parent_task_blocks_the_child():
    items = [row("a1", "p1", "quarantined"), row("b1", "p2", "ready", ["a1"])]
    assert launchable_phases(items, "ticket") == {}


def test_done_need_adds_no_parent_edge():
    items = [row("a1", "p1", "done"), row("a2", "p1", "running"), row("b1", "p2", "ready", ["a1"])]
    assert _phase_parents(items) == {"p1": set(), "p2": set()}
    assert launchable_phases(items, "task") == {"p2": ["b1"]}


def test_arguments_are_not_mutated():
    items = [row("a1", "p1", "done"), row("b1", "p2", "ready", ["a1"])]
    before = [dict(item, needs=list(item["needs"])) for item in items]
    launchable_phases(items, "task")
    assert items == before
