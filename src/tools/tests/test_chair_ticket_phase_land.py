from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_types import Facts


def _initiative() -> dict:
    return {"id": "a", "repo": "r", "started": True, "ready_tasks": [], "landed": set(), "land_at": "ticket"}


def _approved(task: str, phase: str) -> dict:
    return {
        "id": task, "initiative": "a", "repo": "r", "phase": phase, "phase_done": True, "needs": [],
        "run": "run-1", "needs_fetch": False, "merged": True,
    }


def _row(phase: str, *tasks: tuple[str, str]) -> dict:
    return {"initiative": "a", "phase": phase, "tasks": list(tasks)}


def _facts(approved: list[dict], rows: list[dict]) -> Facts:
    return {"approved": approved, "initiatives": [_initiative()], "phase_tasks": rows}  # type: ignore[typeddict-item]


def _kinds(facts: Facts) -> list[tuple[str, str]]:
    return [(a["kind"], a.get("phase", "")) for a in plan_lands(facts)]


def test_a_done_phase_one_of_two_plans_a_land_phase_and_no_deferred_action() -> None:
    facts = _facts([_approved("a-p1", "p1")], [_row("p1", ("a-p1", "approved")), _row("p2", ("a-p2", "ready"))])
    assert _kinds(facts) == [("land_phase", "p1")]


def test_a_phase_with_a_quarantined_task_plans_nothing() -> None:
    facts = _facts([_approved("a-p1", "p1")], [_row("p1", ("a-p1", "approved"), ("a-q", "quarantined"))])
    assert _kinds(facts) == []


def test_a_phase_with_an_unfinished_task_plans_nothing() -> None:
    facts = _facts([_approved("a-p1", "p1")], [_row("p1", ("a-p1", "approved"), ("a-u", "in_progress"))])
    assert _kinds(facts) == []


def test_the_last_phase_plans_a_land_phase() -> None:
    facts = _facts([_approved("a-p2", "p2")], [_row("p2", ("a-p2", "merged"))])
    assert _kinds(facts) == [("land_phase", "p2")]


def test_a_later_ready_phase_waits_until_the_earlier_one_has_landed() -> None:
    both = _facts(
        [_approved("a-p1", "p1"), _approved("a-p2", "p2")],
        [_row("p1", ("a-p1", "approved")), _row("p2", ("a-p2", "approved"))],
    )
    assert _kinds(both) == [("land_phase", "p1")]
    after = _facts([_approved("a-p2", "p2")], [_row("p1", ("a-p1", "done")), _row("p2", ("a-p2", "approved"))])
    assert _kinds(after) == [("land_phase", "p2")]
