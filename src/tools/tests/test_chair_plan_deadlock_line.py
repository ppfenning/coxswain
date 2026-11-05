from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick, reported_waiting
from agent_tools.chair_read_idle_open import open_idle_stall
from agent_tools.chair_report import format_status, needs_chair_items
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
_LAND = ["cox", "runs", "land", "run-1", "--task", "a-p1", "--repo", "r", "--apply", "--no-claim"]
_LINE = "a-p2 needs approved a-p1; land_at defers that land"


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
    """Phase p1 is approved and deferred; a-p2 in the last phase waits on a-p1, and a quarantined task in p0 blocks the initiative."""
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


def _next_tick(facts: Facts, actions: list[dict]) -> Facts:
    """The facts of the following tick: the open signature is what `open_idle_stall` reads back from the lines just planned."""
    rows = [a for a in _chair(actions) if "signature" in a]
    signature, _ = open_idle_stall(rows, {r["signature"] for r in rows})
    return {**facts, "idle_stall": {**facts["idle_stall"], "open_signature": signature}}  # type: ignore[typeddict-item]


def test_a_task_waiting_on_a_deferred_approved_task_is_the_only_needs_chair_and_no_waiting_action():
    actions = plan_tick(_deadlock_facts(), _NOW)
    [line] = _chair(actions)
    assert (line["cause"], line["signature"], line["initiative"]) == ("deferred_land_deadlock", "deferred_land:a-p2", "a")
    assert line["command"] == _LAND
    assert line["reason"] == _LINE
    assert _waiting(actions) == []
    assert needs_chair_items([line]) == [f"needs chair: a:deferred_land_deadlock {_LINE}"]


def test_without_the_stall_the_same_facts_still_plan_the_waiting_action():
    assert len(_waiting(plan_tick({**_deadlock_facts(), "idle_stall": None}, _NOW))) == 1


def test_a_need_outside_any_deferred_phase_keeps_the_plain_idle_stall_line():
    [line] = [a for a in _chair(plan_tick(_deadlock_facts(("a-p2", "a-p9")), _NOW)) if a["cause"] == "idle_stall"]
    assert line["signature"] == "blocked_needs:a-p2"


def test_a_deadlocked_task_behind_an_ordinary_blocked_task_is_still_the_one_line():
    facts = _deadlock_facts(("a-x", "a-p9"), ("a-p2", "a-p1"))
    actions = plan_tick(facts, _NOW)
    [line] = _chair(actions)
    assert line["signature"] == "deferred_land:a-p2"
    assert _waiting(actions) == []


def test_a_failed_host_outranks_the_deadlock_and_stays_the_only_stall_line():
    host = {"host": "box1", "ok": False, "detail": "ssh refused"}
    stalls = [a for a in _chair(plan_tick(_deadlock_facts(hosts=[host]), _NOW)) if a["cause"] in ("idle_stall", "deferred_land_deadlock")]
    assert [(a["cause"], a["signature"]) for a in stalls] == [("idle_stall", "host:box1")]


def test_a_second_tick_with_the_deadlock_open_plans_no_second_line():
    facts = _deadlock_facts()
    assert len(_chair(plan_tick(facts, _NOW))) == 1
    again = plan_tick(_next_tick(facts, plan_tick(facts, _NOW)), _NOW)
    assert _chair(again) == []
    assert _waiting(again) == []


def test_a_second_tick_with_a_deadlock_behind_another_blocked_task_plans_no_line():
    facts = _deadlock_facts(("a-x", "a-p9"), ("a-p2", "a-p1"))
    second = _next_tick(facts, plan_tick(facts, _NOW))
    assert second["idle_stall"]["open_signature"] == "deferred_land:a-p2"
    assert _chair(plan_tick(second, _NOW)) == []


def test_an_older_open_idle_stall_row_does_not_bring_the_deadlock_line_back():
    older = {"kind": "needs_chair", "cause": "idle_stall", "signature": "blocked_needs:a-x", "reason": "x"}
    newer = {"kind": "needs_chair", "cause": "deferred_land_deadlock", "signature": "deferred_land:a-p2", "reason": "y"}
    signature, _ = open_idle_stall([older, newer], {"blocked_needs:a-x", "deferred_land:a-p2"})
    facts = _deadlock_facts(("a-x", "a-p9"), ("a-p2", "a-p1"), open_signature=signature)
    assert _chair(plan_tick(facts, _NOW)) == []


def test_an_open_deadlock_row_yields_its_signature_but_another_cause_does_not():
    row = {"kind": "needs_chair", "cause": "deferred_land_deadlock", "signature": "deferred_land:a-p2", "reason": "why"}
    assert open_idle_stall([row], {"deferred_land:a-p2"}) == ("deferred_land:a-p2", "why")
    assert open_idle_stall([{**row, "cause": "stalled"}], {"deferred_land:a-p2"}) == (None, None)


def _status(facts: Facts) -> str:
    """The status line as chair_run builds it: the plan's actions plus `reported_waiting`."""
    return format_status(facts, [*plan_tick(facts, _NOW), *reported_waiting(facts, _NOW)], [], _NOW)


def test_the_status_line_has_the_deadlock_line_and_no_waiting_part():
    line = _status(_deadlock_facts())
    assert f"needs chair: a:deferred_land_deadlock {_LINE}" in line
    assert "waiting:" not in line
    assert "idle_stall" not in line


def test_the_status_line_without_the_stall_keeps_its_waiting_part():
    line = _status({**_deadlock_facts(), "idle_stall": None})
    assert "waiting: a (blocked by p0: 1 quarantined, 0 awaiting land)" in line


def test_a_second_tick_status_line_has_no_waiting_part_and_shows_the_open_diagnosis():
    facts = _deadlock_facts()
    second = _next_tick(facts, plan_tick(facts, _NOW))
    second["idle_stall"]["open_diagnosis"] = _LINE  # type: ignore[typeddict-item]
    line = _status(second)
    assert "waiting:" not in line
    assert f"stall: {_LINE}" in line
    assert "deferred_land_deadlock" not in line


def test_a_need_on_an_approved_task_outside_a_deferred_phase_keeps_the_plain_idle_stall_line():
    facts = _deadlock_facts(("a-x", "a-p3"))
    facts = {**facts, "approved": [*facts["approved"], _approved("a-p3", "p2")]}
    stalls = [a for a in _chair(plan_tick(facts, _NOW)) if a["cause"] in ("idle_stall", "deferred_land_deadlock")]
    assert [(a["cause"], a["signature"]) for a in stalls] == [("idle_stall", "blocked_needs:a-x")]
    assert "waiting: a" in _status(facts)
