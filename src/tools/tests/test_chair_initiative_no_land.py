"""The chair plans no land for an initiative that lands at `initiative`: the lander owns its phases and its one pull request."""
import pytest

from agent_tools.chair_plan import _landing_initiatives
from agent_tools.chair_plan_land import backoff_held, lander_owned, plan_lands
from agent_tools.chair_types import Facts

RUNS = {"p1": "a-run-1", "p2": "a-run-2", "p3": "a-run-3"}


def _initiative(land_at: str = "initiative") -> dict:
    return {"id": "a", "repo": "r", "started": True, "ready_tasks": [], "landed": set(), "land_at": land_at}


def _approved(phase: str, *, done: bool = True) -> dict:
    return {
        "id": f"a-{phase}", "initiative": "a", "repo": "r", "phase": phase, "phase_done": done, "needs": [],
        "run": RUNS[phase], "needs_fetch": False, "merged": True,
    }


def _row(phase: str, *states: str) -> dict:
    return {"initiative": "a", "phase": phase, "tasks": [(f"a-{phase}-{n}" if n else f"a-{phase}", s) for n, s in enumerate(states)]}


def _facts(approved: list[str], rows: list[dict], land_at: str = "initiative", **more) -> Facts:
    return {"approved": [_approved(p) for p in approved], "initiatives": [_initiative(land_at)], "phase_tasks": rows, **more}  # type: ignore[return-value]


def _steward_says_complete() -> dict:
    epic = {"epic_id": "a", "branch_head": "h1", "levels": [], "exited": False, "signature": "",
            "reported": {"epic_complete:h1"}, "land_at": "initiative"}
    return {"steward": {"enabled": True, "epics": [epic]}}


SHAPES = {
    "first phase built": (["p1"], [_row("p1", "merged"), _row("p2", "ready")], {}),
    "every phase built": (["p1", "p2", "p3"], [_row(p, "merged") for p in RUNS], {}),
    "every phase built and the steward reported the epic complete": (
        ["p1", "p2", "p3"], [_row(p, "merged") for p in RUNS], _steward_says_complete(),
    ),
    "a quarantined sibling": (["p1"], [_row("p1", "approved", "quarantined")], {}),
    "an unfinished sibling": (["p1"], [_row("p1", "approved", "in_progress")], {}),
}


@pytest.mark.parametrize("shape", list(SHAPES))
@pytest.mark.parametrize("land_at", ["initiative", "ticket", "level"])
def test_no_land_of_any_kind_is_planned_for_an_initiative_row(shape: str, land_at: str) -> None:
    approved, rows, more = SHAPES[shape]
    facts = _facts(approved, rows, land_at, **more)
    assert plan_lands(facts) == []
    assert lander_owned(facts) >= {("a", p) for p in approved}


def test_an_initiative_is_never_withheld_from_launches_by_a_land() -> None:
    """Nothing is planned, so nothing is 'landing'; and a refusal an old phase land left behind backs nothing off."""
    refusal = {"initiative": "a", "phase": "p1", "run": "a-run-1", "attempts": 3, "last_refused_at": "2026-11-12T10:00:00+00:00"}
    facts = _facts(["p1"], [_row("p1", "merged"), _row("p2", "ready")], land_refusals=[refusal])
    assert "a" not in _landing_initiatives(facts, plan_lands(facts))
    assert backoff_held(facts) == frozenset()


def test_at_task_the_same_built_phase_still_plans_its_land() -> None:
    """`task` is the path that stays as it was: the chair plans those lands on the tick."""
    facts = _facts(["p1"], [_row("p1", "merged"), _row("p2", "ready")], "task")
    assert [(a["kind"], a["phase"], a["run"]) for a in plan_lands(facts)] == [("land_phase", "p1", "a-run-1")]
    assert lander_owned(facts) == frozenset()
    assert "a" in _landing_initiatives(facts, plan_lands(facts))
