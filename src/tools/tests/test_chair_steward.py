from agent_tools.chair_steward import decide, epic_actions, event_key
from agent_tools.courier import EVENTS


def facts(levels, *, epic="e1", head="h1", exited=False, signature="s1", reported=()):
    return {
        "epic_id": epic,
        "branch_head": head,
        "levels": [{"tasks": [{"id": i, "state": s} for i, s in tasks], "merged": merged} for tasks, merged in levels],
        "exited": exited,
        "signature": signature,
        "reported": frozenset(reported),
    }


def kinds(actions):
    return [(a["kind"], a.get("event")) for a in actions]


def test_event_key_scopes():
    assert event_key("needs_attention", {"task": "t1"}) == "needs_attention:t1"
    assert event_key("relaunched", {"signature": "abc"}) == "relaunched:abc"
    assert event_key("stuck", {"signature": "abc"}) == "stuck:abc"
    assert event_key("run_gate", {"head": "h1"}) == "run_gate:h1"
    assert event_key("gate_passed", {"head": "h1"}) == "gate_passed:h1"
    assert event_key("needs_attention", {"task": "t1"}) != event_key("needs_attention", {"task": "t2"})
    assert event_key("run_gate", {"head": "h1"}) != event_key("run_gate", {"head": "h2"})


def test_quarantine_publishes_once():
    f = facts([([("t1", "quarantined")], False)])
    [action] = epic_actions(f)
    assert action["kind"] == "publish"
    assert action["event"] == "needs_attention"
    assert action["topic"] == "e1"
    assert action["fields"] == {"task": "t1"}
    assert epic_actions(facts([([("t1", "quarantined")], False)], reported=[action["key"]])) == []


def test_quarantine_blocks_relaunch_even_when_reported():
    levels = [([("t1", "quarantined"), ("t2", "ready")], False)]
    assert kinds(epic_actions(facts(levels, exited=True))) == [("publish", "needs_attention")]
    assert epic_actions(facts(levels, exited=True, reported=["needs_attention:t1"])) == []


def test_all_approved_unmerged_level_yields_merge_level():
    f = facts([([("t1", "approved"), ("t2", "done"), ("t3", "dropped")], False)])
    assert kinds(epic_actions(f)) == [("merge_level", None)]


def test_level_with_unfinished_task_is_not_merged():
    assert epic_actions(facts([([("t1", "approved"), ("t2", "building")], False)])) == []


def test_only_first_unmerged_level_is_offered():
    f = facts([([("t1", "approved")], False), ([("t2", "approved")], False)])
    [action] = epic_actions(f)
    assert action["fields"]["level"] == "0"


def test_later_level_not_offered_while_earlier_unfinished():
    f = facts([([("t1", "building")], False), ([("t2", "approved")], False)])
    assert epic_actions(f) == []


def test_merged_level_yields_no_second_merge():
    f = facts([([("t1", "done")], True), ([("t2", "approved")], False)])
    [action] = epic_actions(f)
    assert action["kind"] == "merge_level"
    assert action["fields"]["level"] == "1"
    assert epic_actions(facts([([("t1", "done")], True), ([("t2", "building")], False)])) == []


def test_merge_suppresses_relaunch_and_leads_output():
    levels = [([("t1", "approved")], False), ([("t2", "ready")], False)]
    assert kinds(epic_actions(facts(levels, exited=True))) == [("merge_level", None)]
    mixed = [([("t1", "approved")], False), ([("t2", "quarantined")], False)]
    assert kinds(epic_actions(facts(mixed, exited=True))) == [("publish", "needs_attention"), ("merge_level", None)]


def test_exited_epic_with_ready_tasks_relaunches_once_then_sticks():
    levels = [([("t1", "ready")], False)]
    [action] = epic_actions(facts(levels, exited=True))
    assert action["kind"] == "relaunch"
    assert action["fields"] == {"signature": "s1"}
    assert kinds(epic_actions(facts(levels, exited=True, reported=[action["key"]]))) == [("publish", "stuck")]
    both = [action["key"], "stuck:s1"]
    assert epic_actions(facts(levels, exited=True, reported=both)) == []


def test_new_signature_relaunches_again():
    levels = [([("t1", "ready")], False)]
    f = facts(levels, exited=True, signature="s2", reported=["relaunched:s1", "stuck:s1"])
    assert kinds(epic_actions(f)) == [("relaunch", None)]


def test_running_epic_is_left_alone():
    assert epic_actions(facts([([("t1", "ready")], False)], exited=False)) == []


def test_all_merged_yields_run_gate_once_per_head():
    levels = [([("t1", "done")], True)]
    [action] = epic_actions(facts(levels))
    assert action["kind"] == "run_gate"
    assert action["key"] == "run_gate:h1"
    assert epic_actions(facts(levels, reported=["run_gate:h1"])) == []
    assert kinds(epic_actions(facts(levels, head="h2", reported=["run_gate:h1"]))) == [("run_gate", None)]


def test_merged_level_with_approved_task_still_reaches_the_gate():
    assert kinds(epic_actions(facts([([("t1", "approved")], True)]))) == [("run_gate", None)]


def test_live_task_in_merged_level_holds_the_gate():
    assert epic_actions(facts([([("t1", "building")], True)], exited=True)) == []
    assert kinds(epic_actions(facts([([("t1", "quarantined")], True)], exited=True))) == [
        ("publish", "needs_attention")
    ]
    reported = ["needs_attention:t1"]
    assert epic_actions(facts([([("t1", "quarantined")], True)], exited=True, reported=reported)) == []


def test_ready_task_in_merged_level_relaunches_instead_of_stalling():
    assert kinds(epic_actions(facts([([("t1", "ready")], True)], exited=True))) == [("relaunch", None)]


def test_ready_task_in_later_level_relaunches_an_exited_epic():
    f = facts([([("t1", "building")], False), ([("t2", "ready")], False)], exited=True)
    assert kinds(epic_actions(f)) == [("relaunch", None)]


def test_event_key_is_total_and_orders_fields_by_name():
    assert event_key("level_merged", {"level": "1", "head": "h1"}) == "level_merged:h1:1"
    assert event_key("epic_complete", {}) == "epic_complete"


def test_epic_with_only_dropped_tasks_yields_nothing():
    assert epic_actions(facts([([("t1", "dropped")], False)], exited=True)) == []
    assert epic_actions(facts([([("t1", "dropped")], True)])) == []


def test_merge_index_is_the_level_position_for_identical_levels():
    f = facts([([("t1", "done")], True), ([("t1", "done")], False), ([("t1", "done")], False)])
    [action] = epic_actions(f)
    assert action["fields"]["level"] == "1"


def test_published_events_are_courier_events():
    quarantined = facts([([("t1", "quarantined")], False)])
    stuck = facts([([("t1", "ready")], False)], exited=True, reported=["relaunched:s1"])
    complete = facts([([("t1", "done")], True)], reported=["gate_passed:h1"])
    events = {a["event"] for f in (quarantined, stuck, complete) for a in epic_actions(f) if a["kind"] == "publish"}
    assert events == {"needs_attention", "stuck", "epic_complete"}
    assert events <= set(EVENTS)


def test_gate_waits_for_unfinished_work():
    assert epic_actions(facts([([("t1", "done")], True), ([("t2", "building")], False)])) == []


def test_gate_passed_yields_epic_complete_once():
    levels = [([("t1", "done")], True)]
    f = facts(levels, reported=["run_gate:h1", "gate_passed:h1"])
    [action] = epic_actions(f)
    assert (action["kind"], action["event"], action["topic"]) == ("publish", "epic_complete", "e1")
    assert epic_actions(facts(levels, reported=["gate_passed:h1", action["key"]])) == []


def test_empty_epics_yield_nothing():
    assert epic_actions(facts([])) == []
    assert epic_actions(facts([([], False)], exited=True)) == []


def test_at_most_one_run_gate_across_epics():
    levels = [([("t1", "done")], True)]
    actions = decide([facts(levels, epic="a"), facts(levels, epic="b")])
    assert [(a["kind"], a["topic"]) for a in actions] == [("run_gate", "a")]
    again = decide([facts(levels, epic="a", reported=["run_gate:h1"]), facts(levels, epic="b")])
    assert [(a["kind"], a["topic"]) for a in again] == [("run_gate", "b")]


def test_decide_keeps_other_epics_actions():
    gate = facts([([("t1", "done")], True)], epic="a")
    merge = facts([([("t2", "approved")], False)], epic="b")
    assert [(a["kind"], a["topic"]) for a in decide([gate, merge])] == [("run_gate", "a"), ("merge_level", "b")]
