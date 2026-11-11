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


def _complete(facts: Facts) -> Facts:
    epic = {
        "epic_id": "a", "branch_head": "h1", "levels": [], "exited": False, "signature": "",
        "reported": {"epic_complete:h1"}, "land_at": "ticket",
    }
    return {**facts, "steward": {"enabled": True, "epics": [epic]}}  # type: ignore[typeddict-item]


def test_a_done_phase_one_of_two_plans_no_land_while_the_epic_is_incomplete() -> None:
    facts = _facts([_approved("a-p1", "p1")], [_row("p1", ("a-p1", "approved")), _row("p2", ("a-p2", "ready"))])
    assert _kinds(facts) == []


def test_a_phase_with_a_quarantined_task_plans_nothing() -> None:
    facts = _facts([_approved("a-p1", "p1")], [_row("p1", ("a-p1", "approved"), ("a-q", "quarantined"))])
    assert _kinds(facts) == []


def test_a_phase_with_an_unfinished_task_plans_nothing() -> None:
    facts = _facts([_approved("a-p1", "p1")], [_row("p1", ("a-p1", "approved"), ("a-u", "in_progress"))])
    assert _kinds(facts) == []


def test_the_last_phase_plans_a_land_phase() -> None:
    facts = _complete(_facts([_approved("a-p2", "p2")], [_row("p2", ("a-p2", "merged"))]))
    assert _kinds(facts) == [("land_phase", "p2")]


def test_only_the_last_ready_phase_lands_once_the_epic_is_complete() -> None:
    both = _facts(
        [_approved("a-p1", "p1"), _approved("a-p2", "p2")],
        [_row("p1", ("a-p1", "approved")), _row("p2", ("a-p2", "approved"))],
    )
    assert _kinds(both) == []
    assert _kinds(_complete(both)) == [("land_phase", "p2")]


def _four_phases(reported: set[str] | None) -> Facts:
    runs = {"p1": "a-run-2", "p2": "a-run-3", "p3": "a-run-3", "p4": "a-run-4"}
    facts = _facts(
        [{**_approved(f"a-{p}", p), "run": run} for p, run in runs.items()],
        [_row(p, (f"a-{p}", "merged")) for p in runs],
    )
    if reported is None:
        return facts
    epic = {
        "epic_id": "a", "branch_head": "h1", "levels": [], "exited": False, "signature": "",
        "reported": reported, "land_at": "ticket",
    }
    return {**facts, "steward": {"enabled": True, "epics": [epic]}}  # type: ignore[typeddict-item]


def test_a_complete_ticket_epic_lands_from_its_last_phase_run() -> None:
    plan = plan_lands(_four_phases({"epic_complete:h1"}))
    assert [(a["kind"], a["phase"], a["run"], a["task_runs"]) for a in plan] == [
        ("land_phase", "p4", "a-run-4", {"a-p4": "a-run-4"})
    ]


def test_a_ticket_epic_not_reported_complete_plans_no_land() -> None:
    assert plan_lands(_four_phases({"gate_passed:h1"})) == []
    assert plan_lands(_four_phases(None)) == []


def test_a_complete_epic_whose_last_phase_is_unfinished_plans_nothing() -> None:
    facts = _four_phases({"epic_complete:h1"})
    unfinished = [{**t, "phase_done": t["phase"] != "p4"} for t in facts["approved"]]
    assert plan_lands({**facts, "approved": unfinished}) == []  # type: ignore[typeddict-item]
