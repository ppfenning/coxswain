"""H7.2: a held task reaches the feed with its `needs you:` reason, beside its unchanged `blocked` state."""

from agent_tools import dash_board, dash_feed

_REASON = "needs you: no progress: runs demo-54, demo-55 ended the same way"


def _row(task_id, state, extra=None):
    return {"kind": "task", "initiative": "demo", "phase": "p1", "task_id": task_id, "state": state, "needs": [],
            "extra": extra or {}}


_ROWS = [
    _row("demo-t1", "blocked", {"hold_reason": _REASON}),
    _row("demo-t2", "blocked"),
    _row("demo-t3", "blocked", {"hold_reason": "not a person's mark"}),
    _row("demo-t4", "ready", {"hold_reason": _REASON}),
]


def test_needs_you_is_the_hold_reason_of_a_held_task_only():
    assert [dash_board.needs_you(r) for r in _ROWS] == [_REASON, "", "", ""]


def test_the_initiative_phases_carry_needs_you_on_the_held_task_and_leave_the_others_as_they_were():
    (phase,) = dash_feed._initiative_phases(_ROWS, "demo", set(), set())
    assert phase["tasks"] == [
        {"id": "demo-t1", "state": "blocked", "needs_you": _REASON},
        {"id": "demo-t2", "state": "blocked"},
        {"id": "demo-t3", "state": "blocked"},
        {"id": "demo-t4", "state": "ready"},
    ]


def test_the_board_task_carries_needs_you_and_keeps_its_status_and_counts():
    states = {("demo", "p1", r["task_id"]): "blocked" for r in _ROWS[:2]}
    board = dash_board.build_board(_ROWS[:2], states, {}, {"demo": None}, {}, {})
    (task_held, task_blocked) = board["epics"][0]["initiatives"][0]["phases"][0]["tasks"]
    assert (task_held["status"], task_held["needs_you"]) == ("blocked", _REASON)
    assert "needs_you" not in task_blocked
    assert board["epics"][0]["counts"]["blocked"] == 2
