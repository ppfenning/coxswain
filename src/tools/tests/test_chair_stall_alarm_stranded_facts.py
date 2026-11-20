"""`stranded_alarm_facts`: which stranded rows feed the stall alarm, and the time each was first seen."""

from agent_tools.chair_facts_derive import stranded_alarm_facts

_ROW = {"run": None, "task": "t1", "phase": "p1", "branch": "epic/ship/p1", "remedy": "re-ground", "expected": False}
_QUEUE = [
    {"kind": "task", "initiative": "ship", "task_id": "t1", "phase": "p1", "state": "approved", "updated_at": "2026-11-20T10:00:00Z"},
    {"kind": "task", "initiative": "ship", "task_id": "t2", "phase": "p1", "state": "ready", "updated_at": "2026-11-20T11:00:00Z"},
]


def test_a_stranded_row_with_no_run_takes_its_initiative_and_time_from_the_approved_store_row():
    assert stranded_alarm_facts([_ROW], _QUEUE, set(), set()) == {"t1": "2026-11-20T10:00:00Z"}


def test_a_task_the_store_does_not_hold_approved_is_left_out():
    assert stranded_alarm_facts([{**_ROW, "task": "t2"}], _QUEUE, set(), set()) == {}


def test_an_expected_row_is_left_out():
    assert stranded_alarm_facts([{**_ROW, "expected": True}], _QUEUE, set(), set()) == {}


def test_an_initiative_with_a_live_run_is_left_out():
    assert stranded_alarm_facts([_ROW], _QUEUE, {"ship"}, set()) == {}


def test_a_task_its_phase_land_handles_this_tick_is_left_out():
    assert stranded_alarm_facts([_ROW], _QUEUE, set(), {("ship", "t1")}) == {}
