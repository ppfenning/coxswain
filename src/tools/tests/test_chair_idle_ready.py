from datetime import UTC, datetime

from agent_tools.chair_idle_ready import IDLE_READY_MINUTES, plan_idle_ready
from agent_tools.chair_read_idle_open import open_idle_stall

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
LONG_AGO = "2026-11-09T11:00:00+00:00"
JUST_NOW = "2026-11-09T11:40:00+00:00"
PAIR = [
    {"kind": "needs_chair", "initiative": "a", "other": "b", "cause": "steer_deferred", "reason": "x"},
    {"kind": "needs_chair", "initiative": "b", "other": "a", "cause": "steer_deferred", "reason": "y"},
]


def facts(live_runs: int = 0, last: str | None = LONG_AGO, open_signature: str | None = None) -> dict:
    return {
        "dispatch": {"max_in_flight": 2, "live_runs": live_runs, "hosts": [{"name": "h1", "live_runs": 0}]},
        "initiatives": [{"id": "a", "ready_tasks": [{"id": "t1"}]}, {"id": "b", "ready_tasks": [{"id": "t2"}]}],
        "idle_stall": {"last_progress_at": last, "open_signature": open_signature},
    }


def test_the_threshold_is_thirty_minutes():
    assert IDLE_READY_MINUTES == 30


def test_a_quiet_deadlock_fires_one_action_naming_the_cycle():
    (action,) = plan_idle_ready(facts(), PAIR, NOW)
    assert action["kind"] == "needs_chair"
    assert action["cause"] == "idle_ready"
    assert action["initiative"] == "a"
    assert action["signature"] == "idle_ready:a,b"
    assert action["reason"] == "Idle 30 minutes with ready work and no launch. a waits on b; b waits on a."


def test_the_same_signature_open_returns_nothing():
    (first,) = plan_idle_ready(facts(), PAIR, NOW)
    assert plan_idle_ready(facts(open_signature=first["signature"]), PAIR, NOW) == []


def test_a_different_set_of_initiatives_fires_again():
    third = [{"kind": "needs_chair", "initiative": "c", "other": "a", "cause": "steer_deferred"}]
    (action,) = plan_idle_ready(facts(open_signature="idle_ready:a,b"), [*PAIR, *third], NOW)
    assert action["signature"] == "idle_ready:a,b,c"


def test_without_a_steer_pair_the_ready_initiatives_are_named():
    (action,) = plan_idle_ready(facts(), [], NOW)
    assert action["signature"] == "idle_ready:a,b"
    assert "a has ready work" in action["reason"]


def test_one_live_run_returns_nothing():
    assert plan_idle_ready(facts(live_runs=1), PAIR, NOW) == []


def test_a_host_with_a_live_lane_returns_nothing():
    busy = facts()
    busy["dispatch"]["hosts"] = [{"name": "h1", "live_runs": 1}]
    assert plan_idle_ready(busy, PAIR, NOW) == []


def test_under_thirty_minutes_returns_nothing():
    assert plan_idle_ready(facts(last=JUST_NOW), PAIR, NOW) == []


def test_a_missing_stamp_returns_nothing():
    assert plan_idle_ready(facts(last=None), PAIR, NOW) == []


def test_no_ready_work_returns_nothing():
    idle = facts()
    idle["initiatives"] = [{"id": "a", "ready_tasks": []}]
    assert plan_idle_ready(idle, PAIR, NOW) == []


def test_a_launch_among_the_actions_returns_nothing():
    for kind in ("launch_epic", "relaunch", "retry", "rescue"):
        assert plan_idle_ready(facts(), [*PAIR, {"kind": kind, "initiative": "a"}], NOW) == []


def test_the_open_row_reader_accepts_the_idle_ready_cause():
    row = {"kind": "needs_chair", "cause": "idle_ready", "signature": "idle_ready:a,b", "reason": "r", "ts": LONG_AGO}
    assert open_idle_stall([row], {"idle_ready:a,b"}) == ("idle_ready:a,b", "r")
