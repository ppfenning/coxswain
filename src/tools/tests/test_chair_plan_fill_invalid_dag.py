from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_report import needs_chair_items
from agent_tools.chair_types import Facts


def _init(id: str, started: bool = True, dag_problem: str = "") -> dict:
    return {
        "id": id,
        "started": started,
        "ready_tasks": [{"id": "t1", "needs": []}],
        "landed": set(),
        **({"dag_problem": dag_problem} if dag_problem else {}),
    }


def _facts(*initiatives: dict) -> Facts:
    return {  # type: ignore[return-value]
        "lease": {"holder": "h", "host": "x", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {
            "hard_stop": False,
            "weekly_fraction": 0.0,
            "hard_stop_fraction": 1.0,
            "launch_cap": 4,
            "go_degraded": False,
        },
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": list(initiatives),
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": False,
    }


def test_an_initiative_with_a_dag_problem_launches_no_epic_but_a_valid_one_beside_it_does():
    facts = _facts(_init("bad", dag_problem="t2 needs t1"), _init("good"))
    assert plan_fill(facts, 3) == [{"kind": "launch_epic", "initiative": "good"}]


def test_an_unstarted_initiative_with_a_dag_problem_launches_no_epic():
    assert plan_fill(_facts(_init("bad", started=False, dag_problem="t2 needs t1")), 3) == []


def test_the_invalid_dag_is_reported_once_in_the_needs_chair_line():
    facts = _facts(_init("bad", started=False, dag_problem="t2 needs t1"))
    assert needs_chair_items(plan_recover(facts)) == ["needs chair: bad:invalid_dag (t2 needs t1)"]
