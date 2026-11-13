import copy

from agent_tools.chair_approval_hold import plan_release, without_released


def _hold(task_id: str, ticks: int, initiative: str = "x", surfaces: list[str] | None = None) -> dict:
    hold = {"initiative": initiative, "repo": "/r", "task_id": task_id, "run": "run1", "ticks": ticks}
    return {**hold, "surfaces": surfaces} if surfaces is not None else hold


def _approved(task_id: str, initiative: str = "x", merged: bool = False) -> dict:
    return {"id": task_id, "initiative": initiative, "merged": merged}


def test_five_ticks_releases_nothing():
    assert plan_release({"approval_holds": [_hold("a", 5)]}) == []


def test_six_ticks_releases_with_a_plain_reason():
    assert plan_release({"approval_holds": [_hold("a", 6)]}) == [
        {
            "kind": "release_approvals",
            "initiative": "x",
            "task_ids": ["a"],
            "reason": "approval unmerged for 6 ticks; rebuild on the current main",
        }
    ]


def test_two_stuck_tasks_of_one_initiative_give_one_action_in_facts_order():
    facts = {"approval_holds": [_hold("b", 7), _hold("c", 2), _hold("a", 6), _hold("d", 9, "y")]}
    assert [(a["initiative"], a["task_ids"]) for a in plan_release(facts)] == [("x", ["b", "a"]), ("y", ["d"])]


def test_a_merged_task_is_never_released():
    facts = {"approval_holds": [_hold("a", 9)], "approved": [_approved("a", merged=True)]}
    assert plan_release(facts) == []


def test_the_limit_from_facts_beats_the_default():
    facts = {"approval_holds": [_hold("a", 3)]}
    assert plan_release(facts) == []
    assert [a["task_ids"] for a in plan_release({**facts, "approval_hold_ticks": 3})] == [["a"]]


def test_a_live_initiatives_entry_is_untouched():
    facts = {
        "approval_holds": [_hold("a", 6, surfaces=["a.rs"])],
        "running": [{"id": "x", "repo": "/r", "surfaces": ["a.rs", "b.rs"], "live": True}],
    }
    assert without_released(facts, plan_release(facts))["running"] == facts["running"]


def test_a_non_live_entry_loses_only_the_released_surfaces():
    facts = {
        "approval_holds": [_hold("a", 6, surfaces=["a.rs", "s.rs"]), _hold("b", 1, surfaces=["b.rs", "s.rs"])],
        "running": [{"id": "x", "repo": "/r", "surfaces": ["a.rs", "b.rs", "s.rs"], "live": False}],
    }
    assert without_released(facts, plan_release(facts))["running"][0]["surfaces"] == ["b.rs", "s.rs"]


def test_a_non_live_entry_is_removed_when_all_its_holds_are_released():
    facts = {
        "approval_holds": [_hold("a", 6, surfaces=["a.rs"]), _hold("b", 8, surfaces=["b.rs"])],
        "running": [
            {"id": "x", "repo": "/r", "surfaces": ["a.rs", "b.rs"], "live": False},
            {"id": "y", "repo": "/r", "surfaces": ["c.rs"], "live": False},
        ],
    }
    assert [e["id"] for e in without_released(facts, plan_release(facts))["running"]] == ["y"]


def test_without_released_does_not_mutate_its_input():
    facts = {
        "approval_holds": [_hold("a", 6, surfaces=["a.rs"])],
        "running": [{"id": "x", "repo": "/r", "surfaces": ["a.rs"], "live": False}],
    }
    before = copy.deepcopy(facts)
    without_released(facts, plan_release(facts))
    assert facts == before
