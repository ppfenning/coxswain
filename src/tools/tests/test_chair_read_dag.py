from dataclasses import replace

from test_chair_facts import NOW, _deps

from agent_tools.chair_facts import gather_facts
from agent_tools.chair_read_dag import dag_problems


def _row(initiative: str, task: str, needs: list[str] | None = None, state: str = "ready") -> dict:
    return {"initiative": initiative, "id": task, "needs": needs or [], "state": state, "phase": "p1"}


def test_a_dangling_need_names_the_task_and_the_missing_need_for_that_initiative_only():
    rows = [_row("i", "a2", ["a9"]), _row("j", "b1")]
    assert dag_problems(rows) == {"i": "a2 needs a9"}


def test_a_need_on_a_done_task_in_another_initiative_is_not_a_problem():
    rows = [_row("i", "a1", state="done"), _row("j", "b1", ["a1"])]
    assert dag_problems(rows) == {}


def test_a_valid_initiative_has_no_entry():
    assert dag_problems([_row("i", "a"), _row("i", "b", ["a"])]) == {}


def test_a_need_on_a_dropped_task_reads_dropped():
    rows = [_row("i", "a", state="dropped"), _row("i", "b", ["a"])]
    assert dag_problems(rows) == {"i": "b needs a (dropped)"}


def test_a_cycle_reads_the_path():
    rows = [_row("i", "a", ["b"]), _row("i", "b", ["a"])]
    assert dag_problems(rows) == {"i": "cycle: a -> b -> a"}


def _gathered(rows: list[dict] | None) -> dict:
    base = _deps()
    docket = {
        "initiatives": [
            {"id": n, "started": False, "ready_tasks": [{"id": "x", "phase": "p1", "needs": []}], "landed": []}
            for n in ("i", "j")
        ],
        "busy_lanes": 0,
        "max_in_flight": 2,
    }
    deps = replace(base, docket=lambda: docket, tickets=(lambda: rows) if rows is not None else None)
    return {i["id"]: i for i in gather_facts(deps, NOW)["initiatives"]}


def test_the_gathered_facts_of_an_invalid_initiative_carry_dag_problem_and_a_valid_one_does_not():
    got = _gathered([_row("i", "a2", ["a9"]), _row("j", "b1")])
    assert got["i"]["dag_problem"] == "a2 needs a9"
    assert "dag_problem" not in got["j"]


def test_no_ticket_rows_set_no_dag_problem():
    assert all("dag_problem" not in i for i in _gathered(None).values())


def test_a_cycle_between_phases_with_no_task_cycle_is_the_initiatives_problem():
    # a (p1) needs b (p2) and c (p2) needs d (p1): no task waits on itself, but neither phase can merge first.
    rows = [
        {**_row("i", "a", ["b"]), "phase": "p1"}, {**_row("i", "b"), "phase": "p2"},
        {**_row("i", "c", ["d"]), "phase": "p2"}, {**_row("i", "d"), "phase": "p1"},
        _row("j", "x"),
    ]
    assert dag_problems(rows) == {"i": "phase cycle: p1 -> p2 -> p1"}


def test_phases_in_order_are_not_a_problem():
    rows = [{**_row("i", "a"), "phase": "p1"}, {**_row("i", "b", ["a"]), "phase": "p2"}]
    assert dag_problems(rows) == {}
