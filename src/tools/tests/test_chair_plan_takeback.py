from agent_tools.chair_plan import _lease_gate, plan_tick_held

_OTHER = {"holder": "session-1", "host": "laptop", "epoch": 7, "mine": False, "released": False, "stale": False, "expired": False, "until": ""}
_MINE = {**_OTHER, "holder": "a", "host": "h", "mine": True}


def test_session_held_lease_plans_a_takeback_alone():
    actions = _lease_gate({**_OTHER, "session_held": True})
    assert [a["kind"] for a in actions] == ["take_lease"]
    assert actions[0]["reason"] == "takeback from session-1"
    assert actions[0]["reason"].startswith("takeback from")


def test_absent_session_held_still_stands_by():
    assert _lease_gate(_OTHER) == [{"kind": "standby", "holder": "session-1", "host": "laptop"}]


def test_window_lease_with_session_held_false_still_stands_by():
    lease = {**_OTHER, "until": "2026-11-10T13:00:00+00:00", "session_held": False}
    assert _lease_gate(lease) == [
        {"kind": "standby", "holder": "session-1", "host": "laptop", "until": "2026-11-10T13:00:00+00:00"}
    ]


def test_mine_lease_plans_normally():
    assert _lease_gate(_MINE) is None
    plan = plan_tick_held(_facts(_MINE))
    assert not [a for a in plan.actions if a["kind"] in ("take_lease", "standby")]


def _facts(lease):
    return {
        "lease": lease,
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
