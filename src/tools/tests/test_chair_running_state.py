from agent_tools.chair_facts import running_initiatives
from agent_tools.chair_types import Action


def _item(initiative: str, task: str, state: str, surfaces: list[str], merged: bool = False, phase: str = "p1") -> dict:
    return {
        "initiative": initiative,
        "id": task,
        "state": state,
        "surfaces": surfaces,
        "merged": merged,
        "phase": phase,
    }


def test_a_live_initiative_is_running():
    items = [_item("x", "a", "ready", ["a.rs"])]
    got = running_initiatives({"x"}, items, {"x": "/r"})
    assert got == [{"id": "x", "repo": "/r", "surfaces": ["a.rs"], "live": True, "state": "running"}]


def test_a_non_live_initiative_with_an_approved_unmerged_item_is_awaiting_land():
    items = [_item("x", "a", "approved", ["a.rs"])]
    got = running_initiatives(set(), items, {"x": "/r"}, unlanded={"x"})
    assert got == [{"id": "x", "repo": "/r", "surfaces": ["a.rs"], "live": False, "state": "awaiting_land"}]


def test_a_non_live_initiative_with_only_a_merged_approved_item_has_no_entry():
    items = [_item("x", "a", "approved", ["a.rs"], merged=True)]
    assert running_initiatives(set(), items, {"x": "/r"}, unlanded={"x"}) == []


def test_a_live_phase_entry_is_running():
    items = [_item("x", "a", "in_progress", ["a.rs"])]
    got = running_initiatives(set(), items, {"x": "/r"}, live_phases=[("x", "p1")])
    assert got == [{"id": "x", "repo": "/r", "surfaces": ["a.rs"], "phase": "p1", "live": True, "state": "running"}]


def test_an_action_may_carry_partner_state():
    action: Action = {
        "kind": "steer_clear",
        "initiative": "y",
        "other": "x",
        "paths": ["a.rs"],
        "partner_state": "awaiting_land",
    }
    assert action["partner_state"] == "awaiting_land"
