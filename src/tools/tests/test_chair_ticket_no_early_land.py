from agent_tools.chair_plan import _landing_initiatives
from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_types import Facts

_RUNS = {"p1": "a-run-1", "p2": "a-run-2", "p3": "a-run-3"}


def _initiative() -> dict:
    return {"id": "a", "repo": "r", "started": True, "ready_tasks": [], "landed": set(), "land_at": "ticket"}


def _approved(phase: str) -> dict:
    return {
        "id": f"a-{phase}", "initiative": "a", "repo": "r", "phase": phase, "phase_done": True, "needs": [],
        "run": _RUNS[phase], "needs_fetch": False, "merged": True,
    }


def _row(phase: str, state: str) -> dict:
    return {"initiative": "a", "phase": phase, "tasks": [(f"a-{phase}", state)]}


def _facts(done: list[str], complete: bool) -> Facts:
    rows = [_row(p, "merged" if p in done else "ready") for p in _RUNS]
    facts = {"approved": [_approved(p) for p in done], "initiatives": [_initiative()], "phase_tasks": rows}
    if complete:
        epic = {
            "epic_id": "a", "branch_head": "h1", "levels": [], "exited": False, "signature": "",
            "reported": {"epic_complete:h1"}, "land_at": "ticket",
        }
        facts = {**facts, "steward": {"enabled": True, "epics": [epic]}}
    return facts  # type: ignore[return-value]


def test_an_incomplete_ticket_epic_plans_no_land_for_its_finished_first_phase() -> None:
    assert plan_lands(_facts(["p1"], complete=False)) == []


def test_a_complete_ticket_epic_plans_one_land_for_its_last_phase() -> None:
    plan = plan_lands(_facts(["p1", "p2", "p3"], complete=True))
    assert [(a["kind"], a["phase"], a["run"]) for a in plan] == [("land_phase", "p3", "a-run-3")]


def test_an_incomplete_ticket_epic_is_not_withheld_from_launching_its_p3_task() -> None:
    facts = _facts(["p1"], complete=False)
    ready = {**_initiative(), "ready_tasks": [{"id": "a-p3", "phase": "p3"}]}
    facts = {**facts, "initiatives": [ready]}  # type: ignore[typeddict-item]
    assert "a" not in _landing_initiatives(facts, plan_lands(facts))
