from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick, reported_waiting
from agent_tools.chair_read_idle_open import open_idle_stall
from agent_tools.chair_report import format_status
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


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
        "run_exited": {"a": True},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": "2026-11-04T11:00:00+00:00",
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _idle(*blocked: tuple[str, str], open_signature: str | None = None, hosts: list[dict] | None = None) -> dict:
    return {
        "free_lanes": 1, "ready": 1, "queued": 0, "last_progress_at": "2026-11-04T11:40:00Z", "stall_minutes": 15,
        "hosts": hosts or [], "empty_stubs": [], "lands_waiting": [],
        "blocked_ready": [{"task": task, "unlanded_needs": [need]} for task, need in blocked],
        "open_signature": open_signature, "open_diagnosis": None,
    }


def _approved(task: str, phase: str) -> dict:
    return {
        "id": task, "initiative": "a", "repo": "r", "phase": phase, "phase_done": True, "needs": [],
        "run": "run-1", "needs_fetch": False, "merged": True,
    }


def _deadlock_facts(*blocked: tuple[str, str], **idle) -> Facts:
    """a-p2 in the last phase waits on a-p1; a quarantined task in p0 blocks the initiative. No phase's land is deferred any more."""
    waiting = {"id": "a-p2", "phase": "p2", "needs": ["a-p1"]}
    return _facts(
        initiatives=[{"id": "a", "repo": "r", "started": True, "ready_tasks": [waiting], "landed": set(), "land_at": "ticket"}],
        approved=[_approved("a-p1", "p1")],
        phase_tasks=[
            {"initiative": "a", "phase": "p0", "tasks": [("a-p0", "quarantined")]},
            {"initiative": "a", "phase": "p1", "tasks": [("a-p1", "approved")]},
            {"initiative": "a", "phase": "p2", "tasks": [("a-p2", "ready")]},
        ],
        idle_stall=_idle(*(blocked or (("a-p2", "a-p1"),)), **idle),
    )


def _chair(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a["kind"] == "needs_chair"]


def _waiting(actions: list[dict]) -> list[dict]:
    return [a for a in actions if a["kind"] == "waiting"]


def test_a_need_on_an_approved_task_is_no_deadlock_because_no_land_is_deferred():
    actions = plan_tick(_deadlock_facts(), _NOW)
    assert "idle_stall" in [a["cause"] for a in _chair(actions)]
    assert not any(a.get("cause") == "deferred_land_deadlock" for a in actions)


def test_without_the_stall_the_same_facts_still_plan_the_waiting_action():
    assert len(_waiting(plan_tick({**_deadlock_facts(), "idle_stall": None}, _NOW))) == 1


def test_a_need_outside_any_phase_keeps_the_plain_idle_stall_line():
    [line] = [a for a in _chair(plan_tick(_deadlock_facts(("a-p2", "a-p9")), _NOW)) if a["cause"] == "idle_stall"]
    assert line["signature"] == "blocked_needs:a-p2"


def test_a_failed_host_outranks_the_stall_and_stays_the_only_stall_line():
    host = {"host": "box1", "ok": False, "detail": "ssh refused"}
    stalls = [a for a in _chair(plan_tick(_deadlock_facts(hosts=[host]), _NOW)) if a["cause"] in ("idle_stall", "deferred_land_deadlock")]
    assert [(a["cause"], a["signature"]) for a in stalls] == [("idle_stall", "host:box1")]


def test_an_open_deadlock_row_yields_its_signature_but_another_cause_does_not():
    row = {"kind": "needs_chair", "cause": "deferred_land_deadlock", "signature": "deferred_land:a-p2", "reason": "why"}
    assert open_idle_stall([row], {"deferred_land:a-p2"}) == ("deferred_land:a-p2", "why")
    assert open_idle_stall([{**row, "cause": "stalled"}], {"deferred_land:a-p2"}) == (None, None)


def _status(facts: Facts) -> str:
    """The status line as chair_run builds it: the plan's actions plus `reported_waiting`."""
    return format_status(facts, [*plan_tick(facts, _NOW), *reported_waiting(facts, _NOW)], [], _NOW)


def test_the_status_line_without_the_stall_keeps_its_waiting_part():
    line = _status({**_deadlock_facts(), "idle_stall": None})
    assert "waiting: a (blocked by p0: 1 quarantined, 0 awaiting land)" in line


def test_a_need_on_an_approved_task_in_another_phase_keeps_the_plain_idle_stall_line():
    facts = _deadlock_facts(("a-x", "a-p3"))
    facts = {**facts, "approved": [*facts["approved"], _approved("a-p3", "p2")]}
    stalls = [a for a in _chair(plan_tick(facts, _NOW)) if a["cause"] in ("idle_stall", "deferred_land_deadlock")]
    assert [(a["cause"], a["signature"]) for a in stalls] == [("idle_stall", "blocked_needs:a-x")]
    assert "waiting: a" in _status(facts)
