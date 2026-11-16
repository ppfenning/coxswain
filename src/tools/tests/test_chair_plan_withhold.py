from datetime import UTC, datetime

from agent_tools.chair_facts import quarantine_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_read_stranded import stranded_from_rows
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": None,
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _initiative(id: str, repo: str, started: bool = False) -> dict:
    return {"id": id, "repo": repo, "started": started, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


def _launched(actions: list[dict]) -> set[str]:
    return {a["initiative"] for a in actions if a["kind"] in {"launch_epic", "relaunch"}}


def test_an_initiative_being_landed_gets_no_launch():
    landing = [{"initiative": "a", "phase": "p", "repo": "r1"}]
    facts = _facts(initiatives=[_initiative("a", "r1")], landing=landing)
    assert _launched(plan_tick(facts, _NOW)) == set()


def test_a_started_initiative_being_landed_gets_no_launch():
    landing = [{"initiative": "a", "phase": "p", "repo": "r1"}]
    facts = _facts(initiatives=[_initiative("a", "r1", started=True)], landing=landing)
    assert _launched(plan_tick(facts, _NOW)) == set()


def _plan_after_a_rebuild(extra: dict) -> list[dict]:
    record = {"run": "a-1", "task": "a-t", "phase": "p1", "branch": "b1", "review": {"verdict": "approve"},
              "arbitration": {"verdict": "approve"}, "landed": False}
    row = {"kind": "task", "task_id": "a-t", "initiative": "a", "phase": "p1", "state": "ready", "extra": extra}
    quarantines = quarantine_facts([], stranded_from_rows([row], [record]), [], set(), lambda initiative, task: False)
    return plan_tick(_facts(initiatives=[_initiative("a", "r1")], quarantines=quarantines), _NOW)


def test_a_forced_rebuild_is_relaunched_and_raises_no_stranded_needs_chair():
    actions = _plan_after_a_rebuild({"rebuilt_from": ["a-1"]})
    assert [a["kind"] for a in actions if a["kind"] in {"launch_epic", "needs_chair"}] == ["launch_epic"]


def test_the_same_ready_task_without_the_rebuild_marker_is_stranded_and_withheld():
    actions = _plan_after_a_rebuild({})
    assert [a["kind"] for a in actions if a["kind"] in {"launch_epic", "needs_chair"}] == ["needs_chair"]


def test_the_same_initiative_launches_when_no_repo_is_landing():
    facts = _facts(initiatives=[_initiative("a", "r1")], landing=[])
    assert _launched(plan_tick(facts, _NOW)) == {"a"}


def test_a_land_phase_planned_this_tick_withholds_its_initiative_and_leaves_another_repo_alone():
    approved = [
        {
            "id": "t1", "initiative": "a", "repo": "r1", "phase": "p", "phase_done": True, "needs": [], "run": "a-1",
            "needs_fetch": False,
        }
    ]
    facts = _facts(approved=approved, initiatives=[_initiative("a", "r9"), _initiative("b", "r2")])
    actions = plan_tick(facts, _NOW)
    assert [a["initiative"] for a in actions if a["kind"] == "land_phase"] == ["a"]
    assert _launched(actions) == {"b"}
