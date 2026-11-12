from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent_tools import pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import (
    FactsDeps,
    epic_facts,
    gather_facts,
    land_at_facts,
    resolve_steward_enabled,
    resolve_steward_scope,
    steward_asked_for,
    steward_initiative_ids,
    steward_plan_facts,
)
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
def test_an_explicit_true_is_asked_for_and_still_turns_nothing_on(value):
    assert steward_asked_for(value) is True
    assert resolve_steward_enabled(value) is False


@pytest.mark.parametrize("value", [None, 5, True, [], "", "all", "banana"])
def test_the_steward_scope_is_all_for_an_absent_or_unknown_value(value):
    assert resolve_steward_scope(value) == "all"


@pytest.mark.parametrize("value", ["epics", " Epics "])
def test_the_steward_scope_is_epics_ignoring_case_and_space(value):
    assert resolve_steward_scope(value) == "epics"


def _planned(rows, scope=None) -> list[str]:
    """The ids the scope selects, from the pure functions: `gather_facts` no longer plans the retired steward."""
    return steward_initiative_ids(resolve_steward_scope(scope), epic_facts(rows, {}), rows)


def _initiative(name, **extra):
    return {"kind": "initiative", "initiative": name, "task_id": name, "phase": "", "state": "todo", "extra": extra}


def _task(initiative, task_id, state):
    return {"kind": "task", "initiative": initiative, "task_id": task_id, "phase": "p1", "state": state}


LOOSE = [_initiative("solo"), _task("solo", "a", "ready")]
LANDED = [_initiative("done"), _task("done", "a", "done"), _task("done", "b", "dropped")]


def test_an_initiative_with_no_epic_is_planned_under_the_default_scope():
    assert _planned(LOOSE) == ["solo"]


def test_an_initiative_with_no_epic_is_skipped_under_the_epics_scope():
    assert _planned(LOOSE, "epics") == []


@pytest.mark.parametrize("scope", [None, "epics"])
def test_a_landed_initiative_is_skipped_under_both_scopes(scope):
    assert _planned(LANDED, scope) == []


@pytest.mark.parametrize("scope", [None, "epics"])
def test_an_epic_member_is_planned_once_under_both_scopes(scope):
    assert _planned(ROWS, scope) == ["i"]


def test_an_unknown_scope_value_plans_the_same_as_all():
    rows = [*LOOSE, *ROWS, *LANDED]
    assert _planned(rows, "banana") == _planned(rows) == ["i", "solo"]


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


def test_with_the_setting_on_gather_facts_still_has_no_steward_key():
    wired = replace(
        _deps(),
        work_rows=lambda: ROWS,
        run_exited=lambda: {"i": True},
        steward_entries=lambda: ENTRIES,
        ticket_head=lambda initiative: "abc",
        level_merged=lambda initiative, phase: phase == "p2",
    )
    for value in (True, "true"):
        on = gather_facts(replace(wired, steward_enabled=lambda value=value: value, steward_scope=lambda: "epics"), NOW)
        assert "steward" not in on and on == gather_facts(wired, NOW)


def test_steward_plan_facts_fills_levels_merged_state_head_exit_and_reported_keys():
    steward = steward_plan_facts(
        ["i"], ROWS, {"i": True}, lambda initiative: "abc", lambda initiative, phase: phase == "p2", ENTRIES
    )
    (epic,) = steward["epics"]
    assert steward["enabled"] is True
    assert epic["epic_id"] == "i" and epic["branch_head"] == "abc" and epic["exited"] is True
    assert epic["levels"] == [
        {"tasks": [{"id": "a", "state": "ready"}, {"id": "c", "state": "building"}], "merged": True, "phase": "p2"},
        {"tasks": [{"id": "b", "state": "approved"}], "merged": False, "phase": "p10"},
    ]
    assert epic["signature"] == "a=ready,b=approved,c=building"
    assert epic["reported"] == reported_keys(ENTRIES, "i")
    assert epic["land_at"] == "initiative"


def _plan(land_at, landed, level_merged=lambda initiative, phase: False):
    rows = [
        {**_task("i", "a", "approved"), "extra": {"merged": True}},
        _task("i", "b", "done"),
        _task("i", "x", "dropped"),
        {**_task("i", "c", "approved"), "phase": "p2"},
    ]
    (epic,) = steward_plan_facts(["i"], rows, {}, lambda initiative: "h", level_merged, [], land_at, landed)["epics"]
    return epic


def test_at_initiative_a_level_is_merged_by_the_ancestry_callable_not_the_landed_set():
    epic = _plan({"i": "initiative"}, {"i": {"a", "b", "c"}}, lambda initiative, phase: phase == "p2")
    assert [lv["merged"] for lv in epic["levels"]] == [False, True]
    assert epic["land_at"] == "initiative"


def test_at_task_a_fully_landed_level_is_merged_with_no_ticket_branch():
    epic = _plan({"i": "task"}, {"i": {"a", "b"}})
    assert [lv["merged"] for lv in epic["levels"]] == [True, False]
    assert epic["land_at"] == "task"


def test_at_task_a_level_with_an_unlanded_task_is_not_merged():
    assert [lv["merged"] for lv in _plan({"i": "task"}, {"i": {"b"}})["levels"]] == [False, False]


def test_at_task_a_dropped_task_need_not_be_landed():
    assert _plan({"i": "task"}, {"i": {"a", "b"}})["levels"][0]["merged"] is True


def test_an_initiative_missing_from_land_at_reads_initiative_and_one_missing_from_landed_reads_empty():
    epic = _plan({}, {}, lambda initiative, phase: True)
    assert epic["land_at"] == "initiative" and [lv["merged"] for lv in epic["levels"]] == [True, True]
    assert [lv["merged"] for lv in _plan({"i": "task"}, {})["levels"]] == [False, False]


def test_each_level_carries_its_phase_name_in_natural_order():
    assert [lv["phase"] for lv in _plan({"i": "task"}, {})["levels"]] == ["p1", "p2"]


def test_the_resolved_land_at_reaches_the_plan_facts_and_gather_facts_plans_none_of_it():
    deps = replace(
        _deps(),
        steward_enabled=lambda: True,
        work_rows=lambda: ROWS,
        cartridge=lambda: {},
        land_at_values=lambda: {"i": "level"},
        level_merged=lambda initiative, phase: False,
    )
    assert "steward" not in gather_facts(deps, NOW)
    land_at = land_at_facts({}, {"i": "level"}, ["i"])
    (epic,) = steward_plan_facts(["i"], ROWS, {}, lambda initiative: "", lambda initiative, phase: False, [], land_at, {})["epics"]
    assert epic["land_at"] == "initiative"  # the retired level reads as initiative
    assert [lv["merged"] for lv in epic["levels"]] == [False, False]


def _git(answers: dict[str, tuple[int, str]]):
    return lambda args: next((v for k, v in answers.items() if k in args), (1, ""))


def test_level_is_merged_is_true_for_an_ancestor_and_false_for_a_conflict():
    merged = _git({"--is-ancestor": (0, ""), "rev-parse": (0, "t1\n"), "merge-tree": (1, "")})
    conflict = _git({"--is-ancestor": (1, ""), "rev-parse": (0, "t1\n"), "merge-tree": (1, "")})
    assert (level_is_merged("/r", "ticket", "lv", merged), level_is_merged("/r", "ticket", "lv", conflict)) == (True, False)
