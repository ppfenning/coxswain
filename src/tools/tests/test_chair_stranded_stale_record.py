from datetime import UTC, datetime, timedelta

import pytest

from agent_tools import pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import FactsDeps, gather_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)
MINE = lease_holder("s", 7, "h")
IDLE_STALL = {
    "free_lanes": 2, "ready": 1, "queued": 0, "last_progress_at": "2026-11-10T11:44:00Z", "stall_minutes": 15,
    "hosts": [], "empty_stubs": [], "lands_waiting": [], "blocked_ready": [], "open_signature": None, "open_diagnosis": None,
}
# An old run record for task "a" that approved and never landed; the store has since sent the task back.
STALE_RECORD = {
    "run": "i-1", "initiative": "i", "task": "a", "phase": "p1", "branch": "i/a",
    "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"}, "landed": False,
}


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def _row(task_id: str, state: str) -> dict:
    return {
        "kind": "task", "initiative": "i", "task_id": task_id, "phase": "p1", "state": state,
        "needs": [], "title": "", "surfaces": [], "body": "", "extra": {}, "holder": None, "expires_at": None,
    }


def _deps(rows: list[dict]) -> FactsDeps:
    return FactsDeps(
        lease=lambda: {"holder": MINE, "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(1.0, 5),
        weekly=lambda: _window(10.0, 168),
        policy=lambda: POLICY,
        docket=lambda: {"initiatives": [], "busy_lanes": 0, "max_in_flight": 2},
        approved=lambda: [],
        quarantined=lambda: (),
        stranded=lambda: (),
        attempts=lambda: (),
        has_patch=lambda initiative, task: False,
        live_initiatives=lambda: (),
        intake=lambda: [],
        work_store_ready=lambda: True,
        sources_configured=lambda: False,
        session="s",
        pid=7,
        host="h",
        idle_stall=lambda n: IDLE_STALL,
        queue=lambda: rows,
        stranded_records=lambda: [STALE_RECORD],
    )


def _facts(a_state: str) -> Facts:
    return gather_facts(_deps([_row("a", a_state), _row("b", "ready")]), NOW)


def _launched(actions: list[dict]) -> set[str]:
    return {a["initiative"] for a in actions if a["kind"] in ("launch_epic", "relaunch")}


def _withheld_reasons(actions: list[dict]) -> list[str]:
    return [a["reason"] for a in actions if a["kind"] == "unlaunched" and a["initiative"] == "i"]


def test_a_stale_approved_record_for_a_ready_store_task_withholds_nothing():
    facts = _facts("ready")
    actions = plan_tick(facts, NOW)
    assert facts["quarantines"] == []
    assert not [a for a in actions if a["kind"] == "needs_chair" and a["initiative"] == "i" and a.get("cause") == "stranded"]
    assert _launched(actions) == {"i"}
    assert not [r for r in _withheld_reasons(actions) if "quarantined" in r]


def test_an_approved_store_row_keeps_the_stranded_entry():
    facts = _facts("approved")
    assert [(q["initiative"], q["cause"]) for q in facts["quarantines"]] == [("i", "stranded")]


@pytest.mark.parametrize("state", ["dropped", "done"])
def test_a_dropped_or_done_store_row_withholds_nothing(state):
    facts = _facts(state)
    actions = plan_tick(facts, NOW)
    assert facts["quarantines"] == []
    # The initiative is now started, so an unrelated exit check may hold it; the stranded record must not.
    assert not [r for r in _withheld_reasons(actions) if "quarantined" in r or "stranded" in r]
    assert not [a for a in actions if a["kind"] == "needs_chair" and a.get("cause") == "stranded"]
