from datetime import UTC, datetime, timedelta

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
_LIMITS = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": _LIMITS,
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
        "last_housekeeping_at": _NOW.isoformat(),
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _initiative(id: str, surfaces: list[str]) -> dict:
    return {
        "id": id,
        "started": False,
        "ready_tasks": [{"id": f"{id}-t", "needs": []}],
        "landed": set(),
        "repo": "r",
        "ready_surfaces": surfaces,
    }


def _held(id: str, surfaces: list[str], live: bool = False) -> dict:
    return {"id": id, "repo": "r", "surfaces": surfaces, "live": live}


def _cycle(**overrides) -> Facts:
    """A, B and C each want a file another's approval holds; no run is live."""
    return _facts(
        initiatives=[
            _initiative("A", ["a.py", "b.py"]),
            _initiative("B", ["b.py", "c.py"]),
            _initiative("C", ["c.py", "a.py"]),
        ],
        running=[_held("A", ["a.py"]), _held("B", ["b.py"]), _held("C", ["c.py"])],
        **overrides,
    )


def _deferred(plan: list[dict]) -> list[dict]:
    return [
        a
        for a in plan
        if a["kind"] == "steer_clear" or (a["kind"] == "needs_chair" and a.get("cause") == "steer_deferred")
    ]


def _idle(minutes: int, **extra) -> dict:
    """Idle-stall inputs whose own stall threshold is far off, so only the idle_ready rule can fire."""
    return {
        "free_lanes": 0,
        "ready": 3,
        "queued": 0,
        "last_progress_at": (_NOW - timedelta(minutes=minutes)).isoformat(),
        "stall_minutes": 600,
        "hosts": [],
        "empty_stubs": [],
        "lands_waiting": [],
        "blocked_ready": [],
        "open_signature": None,
        "open_diagnosis": None,
        **extra,
    }


def _idle_ready(plan: list[dict]) -> list[dict]:
    return [a for a in plan if a["kind"] == "needs_chair" and a.get("cause") == "idle_ready"]


def test_a_cycle_of_approval_holds_launches_the_first_priority_initiative():
    plan = plan_tick(_cycle(), _NOW)
    assert [a["initiative"] for a in plan if a["kind"] == "launch_epic"] == ["A"]
    assert [a for a in _deferred(plan) if a["initiative"] == "A"] == []
    assert {(a["initiative"], a["other"]) for a in _deferred(plan)} == {("B", "A"), ("C", "A")}


def test_idle_with_ready_work_and_no_launch_raises_one_idle_ready_per_signature():
    idle = _idle(31)
    stalled = {"max_in_flight": 0, "live_runs": 0, "hosts": []}
    plan = plan_tick(_cycle(dispatch=stalled, idle_stall=idle), _NOW)
    raised = _idle_ready(plan)
    assert len(raised) == 1
    assert raised[0]["signature"] == "idle_ready:A,B,C"
    assert all(name in raised[0]["reason"] for name in "ABC")
    again = plan_tick(_cycle(dispatch=stalled, idle_stall={**idle, "open_signature": raised[0]["signature"]}), _NOW)
    assert _idle_ready(again) == []


def test_idle_ready_fires_under_a_hard_stop():
    plan = plan_tick(_cycle(limits={**_LIMITS, "hard_stop": True}, idle_stall=_idle(31)), _NOW)
    assert len(_idle_ready(plan)) == 1
    assert [a for a in plan if a["kind"] == "launch_epic"] == []


def test_an_approval_at_the_hold_limit_is_released_and_its_files_stop_blocking():
    facts = _facts(
        initiatives=[_initiative("B", ["a.py"])],
        running=[_held("A", ["a.py"])],
        approval_holds=[{"initiative": "A", "task_id": "A-t", "ticks": 6, "surfaces": ["a.py"]}],
    )
    plan = plan_tick(facts, _NOW)
    assert [a for a in plan if a["kind"] == "release_approvals"] == [
        {
            "kind": "release_approvals",
            "initiative": "A",
            "task_ids": ["A-t"],
            "reason": "approval unmerged for 6 ticks; rebuild on the current main",
            "epoch": 7,
        }
    ]
    assert [a["initiative"] for a in plan if a["kind"] == "launch_epic"] == ["B"]
    assert _deferred(plan) == []


def test_an_approval_below_the_hold_limit_is_kept():
    facts = _facts(
        initiatives=[_initiative("B", ["a.py"])],
        running=[_held("A", ["a.py"])],
        approval_holds=[{"initiative": "A", "task_id": "A-t", "ticks": 5, "surfaces": ["a.py"]}],
    )
    plan = plan_tick(facts, _NOW)
    assert [a for a in plan if a["kind"] == "release_approvals"] == []
    assert [a for a in plan if a["kind"] == "launch_epic"] == []


def test_a_live_run_in_the_repo_leaves_the_steering_as_it_was():
    facts = _cycle()
    facts["running"][0]["live"] = True
    plan = plan_tick(facts, _NOW)
    assert [a for a in plan if a["kind"] == "launch_epic"] == []
    assert {a["initiative"] for a in _deferred(plan)} == {"A", "B", "C"}
    assert [a for a in plan if a["kind"] == "release_approvals"] == []
    assert _idle_ready(plan) == []
