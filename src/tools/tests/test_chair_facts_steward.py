from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent_tools import pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import FactsDeps, gather_facts, resolve_steward_enabled
from agent_tools.chair_read_steward import level_is_merged, reported_keys, task_signature
from agent_tools.chair_steward import event_key

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
POLICY = pacing.Policy((1.2, 1.5, 2.0), ("deep", "standard", "cheap"), ("high", "low"), 1.0, 0.85)
IDLE = {
    "free_lanes": 0, "ready": 0, "queued": 0, "last_progress_at": None, "stall_minutes": 15, "hosts": [],
    "empty_stubs": [], "lands_waiting": [], "blocked_ready": [], "open_signature": None, "open_diagnosis": None,
}
ROWS = [
    {"kind": "initiative", "initiative": "i", "task_id": "i", "phase": "", "state": "todo", "extra": {"epic": "e"}},
    {"kind": "task", "initiative": "i", "task_id": "a", "phase": "p2", "state": "ready"},
    {"kind": "task", "initiative": "i", "task_id": "b", "phase": "p10", "state": "approved"},
    {"kind": "task", "initiative": "i", "task_id": "c", "phase": "p2", "state": "todo"},
]
ENTRIES = [
    {"id": "1", "event": "needs_attention", "fields": {"task": "a"}, "topic": "i"},
    {"id": "2", "event": "relaunched", "fields": {"signature": "a=ready"}, "topic": "i"},
    {"id": "3", "event": "gate_passed", "fields": {"head": "abc"}, "topic": "i"},
    {"id": "4", "note": "plain message"},
    {"id": "5", "event": "stuck", "fields": {"signature": "x"}, "topic": "other"},
]


def _window(hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), 1.0, 100.0, 0.0, 0)


def _deps() -> FactsDeps:
    return FactsDeps(
        lease=lambda: {"holder": lease_holder("s", 7, "h"), "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(5),
        weekly=lambda: _window(168),
        policy=lambda: POLICY,
        docket=lambda: {"initiatives": [], "busy_lanes": 0, "max_in_flight": 2},
        approved=lambda: [],
        quarantined=lambda: [],
        stranded=lambda: [],
        attempts=lambda: [],
        has_patch=lambda initiative, task: False,
        live_initiatives=lambda: (),
        intake=lambda: [],
        work_store_ready=lambda: True,
        sources_configured=lambda: False,
        session="s",
        pid=7,
        host="h",
        idle_stall=lambda n: IDLE,
    )


@pytest.mark.parametrize("value", [None, "banana", 0, 1, [], "", "yes", "false", False])
def test_the_steward_setting_is_off_for_an_absent_or_malformed_value(value):
    assert resolve_steward_enabled(value) is False


@pytest.mark.parametrize("value", [True, "true", " True "])
def test_the_steward_setting_is_on_for_an_explicit_true(value):
    assert resolve_steward_enabled(value) is True


def test_reported_keys_returns_quarantine_relaunch_and_gate_keys_and_ignores_untyped_entries():
    assert reported_keys(ENTRIES, "i") == {
        event_key("needs_attention", {"task": "a"}),
        event_key("relaunched", {"signature": "a=ready"}),
        event_key("gate_passed", {"head": "abc"}),
    }


def test_reported_keys_without_a_topic_keeps_every_typed_entry():
    assert "stuck:x" in reported_keys(ENTRIES)


def test_task_signature_ignores_order_and_changes_with_a_state():
    a = {"id": "a", "state": "ready"}
    b = {"id": "b", "state": "done"}
    assert task_signature([a, b]) == task_signature([b, a])
    assert task_signature([a, b]) != task_signature([{**a, "state": "building"}, b])


def test_with_the_setting_off_gather_facts_has_no_steward_key_and_is_otherwise_unchanged():
    base = gather_facts(_deps(), NOW)
    off = gather_facts(replace(_deps(), steward_enabled=lambda: "banana", work_rows=None), NOW)
    assert "steward" not in base and off == base


def test_with_the_setting_on_gather_facts_fills_levels_merged_state_head_exit_and_reported_keys():
    deps = replace(
        _deps(),
        steward_enabled=lambda: True,
        work_rows=lambda: ROWS,
        run_exited=lambda: {"i": True},
        steward_entries=lambda: ENTRIES,
        ticket_head=lambda initiative: "abc",
        level_merged=lambda initiative, phase: phase == "p2",
    )
    steward = gather_facts(deps, NOW)["steward"]
    (epic,) = steward["epics"]
    assert steward["enabled"] is True
    assert epic["epic_id"] == "i" and epic["branch_head"] == "abc" and epic["exited"] is True
    assert epic["levels"] == [
        {"tasks": [{"id": "a", "state": "ready"}, {"id": "c", "state": "building"}], "merged": True},
        {"tasks": [{"id": "b", "state": "approved"}], "merged": False},
    ]
    assert epic["signature"] == "a=ready,b=approved,c=building"
    assert epic["reported"] == reported_keys(ENTRIES, "i")


def _git(answers: dict[str, tuple[int, str]]):
    return lambda args: next((v for k, v in answers.items() if k in args), (1, ""))


def test_level_is_merged_is_true_for_an_ancestor_and_false_for_a_conflict():
    merged = _git({"--is-ancestor": (0, ""), "rev-parse": (0, "t1\n"), "merge-tree": (1, "")})
    conflict = _git({"--is-ancestor": (1, ""), "rev-parse": (0, "t1\n"), "merge-tree": (1, "")})
    assert (level_is_merged("/r", "ticket", "lv", merged), level_is_merged("/r", "ticket", "lv", conflict)) == (True, False)
