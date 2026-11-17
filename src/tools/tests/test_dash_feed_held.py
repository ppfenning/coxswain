"""H7.2: a held task reaches the feed with its `needs you:` reason, beside its unchanged `blocked` state."""

import json
from pathlib import Path

from agent_tools import dash_board, dash_feed
from agent_tools.dash_contract import CONTRACT_VERSION

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


# --- the published fixture: its held initiative is these producers' output for these rows ---

_FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_held_v1.json"
_HELD = "needs you: no progress: runs stray-4, stray-5 ended the same way (quarantined: tests fail in p1-fix)"
HELD_ROWS = [
    {"kind": "task", "initiative": "stray-fix", "phase": "p1-fix", "task_id": task, "state": state, "needs": [],
     "extra": extra}
    for task, state, extra in (
        ("stray-fix-patch", "in_progress", {}),
        ("stray-fix-docs", "blocked", {}),
        ("stray-fix-review", "blocked", {"hold_reason": _HELD}),
    )
]
_LIVE = {("stray-fix", "p1-fix")}


def held_board() -> dict:
    states = dash_feed._queue_states(HELD_ROWS, _LIVE, set())
    return dash_board.build_board(HELD_ROWS, states, {"stray-fix-patch": ("stray-1", "host-b")}, {"stray-fix": None},
                                  {"stray-fix": "sf"}, {"stray-fix-patch": 0.5})


def held_phases() -> list[dict]:
    return dash_feed._initiative_phases(HELD_ROWS, "stray-fix", _LIVE, set())


def test_the_held_fixture_s_board_epic_and_queue_phases_are_the_producers_output():
    """Towpath reads this fixture at its pin, so the `needs_you` key it renders is the key the feed sends."""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == CONTRACT_VERSION
    (epic,) = [e for e in data["board"]["epics"] if e["id"] == ""]
    assert epic == held_board()["epics"][0]
    (entry,) = [q for q in data["queue"] if q["initiative"] == "stray-fix"]
    assert entry["phases"] == held_phases()
    held = [t for t in entry["phases"][0]["tasks"] if "needs_you" in t]
    assert held == [{"id": "stray-fix-review", "state": "blocked", "needs_you": _HELD}]
