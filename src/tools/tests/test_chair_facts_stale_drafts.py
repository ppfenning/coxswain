from agent_tools.chair_facts import stale_draft_keys


def test_each_recorded_stale_to_draft_gives_its_initiative_and_sorted_stale_tasks():
    rows = [
        {"action_json": {"kind": "stale_to_draft", "initiative": "a", "stale_tasks": ["a-2", "a-1"], "status": "refused"}},
        {"action_json": '{"kind": "stale_to_draft", "initiative": "b", "stale_tasks": ["b-1"], "status": "done"}'},
        {"action_json": {"kind": "needs_chair", "initiative": "c", "stale_tasks": ["c-1"]}},
        {"action_json": {"kind": "stale_to_draft", "stale_tasks": ["x"]}},
    ]
    assert stale_draft_keys(rows) == ["a|a-1,a-2", "b|b-1"]
