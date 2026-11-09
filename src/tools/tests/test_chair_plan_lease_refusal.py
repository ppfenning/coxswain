from datetime import UTC, datetime, timedelta

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _ago(seconds: int) -> str:
    return (_NOW - timedelta(seconds=seconds)).isoformat()


def _initiative(id: str, started: bool = False) -> dict:
    return {
        "id": id,
        "repo": f"r-{id}",
        "started": started,
        "ready_tasks": [{"id": f"{id}-t", "needs": []}],
        "landed": set(),
    }


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {
            "hard_stop": False,
            "weekly_fraction": 0.1,
            "hard_stop_fraction": 0.9,
            "launch_cap": 5,
            "go_degraded": False,
        },
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [_initiative("a")],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {"a": True},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": _ago(60),
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _streak(first: int, last: int, holder: str = "h1") -> dict:
    return {"a": {"first_at": _ago(first), "last_at": _ago(last), "holder_run": holder}}


def _alive() -> list[dict]:
    return [{"id": "h1", "repo": "r-h1", "surfaces": []}]


def _kinds(facts: Facts) -> list[str]:
    return [a["kind"] for a in plan_tick(facts, _NOW) if a.get("initiative") == "a" and a["kind"] != "unlaunched"]


def _stuck(facts: Facts) -> list[dict]:
    return [a for a in plan_tick(facts, _NOW) if a.get("cause") == "lease_stuck"]


def test_a_gone_holder_plans_the_launch_on_the_next_tick():
    assert _kinds(_facts(lease_refusals=_streak(60, 60))) == ["launch_epic"]


def test_a_live_holder_inside_the_ttl_plans_no_launch_and_no_stuck():
    facts = _facts(lease_refusals=_streak(60, 60), running=_alive())
    assert _kinds(facts) == []
    assert _stuck(facts) == []


def test_a_live_holder_past_the_ttl_plans_one_lease_stuck_and_no_launch():
    facts = _facts(lease_refusals=_streak(130, 10), running=_alive())
    assert _stuck(facts) == [
        {
            "kind": "needs_chair",
            "initiative": "a",
            "cause": "lease_stuck",
            "reason": f"run h1 still holds the lease; launches refused from {_ago(130)} to {_ago(10)}",
            "epoch": 7,
        }
    ]
    assert "launch_epic" not in _kinds(facts)


def test_a_reported_stuck_key_plans_none_and_still_withholds():
    facts = _facts(lease_refusals=_streak(130, 10), running=_alive(), relaunch_loop_reported=["lease_stuck|a|h1"])
    assert _stuck(facts) == []
    assert _kinds(facts) == []


def test_a_new_holder_run_is_reported_again():
    facts = _facts(
        lease_refusals=_streak(130, 10, "h2"),
        running=[{"id": "h2", "repo": "r", "surfaces": []}],
        relaunch_loop_reported=["lease_stuck|a|h1"],
    )
    assert len(_stuck(facts)) == 1


def test_a_gone_holder_long_after_the_refusal_plans_the_launch_with_no_stuck():
    facts = _facts(lease_refusals=_streak(400, 400))
    assert _kinds(facts) == ["launch_epic"]
    assert _stuck(facts) == []


def test_three_refused_rows_do_not_spend_the_launch_budget():
    facts = _facts(lease_refusals=_streak(300, 60), max_launches_per_hour=1, launch_history={"a": {"launches": []}})
    assert _kinds(facts) == ["launch_epic"]


def test_a_live_holder_withholds_a_started_initiatives_relaunch():
    started = [_initiative("a", started=True)]
    held = _facts(initiatives=started, lease_refusals=_streak(60, 60), running=_alive())
    assert _kinds(held) == []
    assert _kinds(_facts(initiatives=started, lease_refusals=_streak(60, 60))) == ["clear_branches", "relaunch"]
