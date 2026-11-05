from test_chair_plan_recover import _facts, _initiative

from agent_tools.chair_facts import quarantine_facts
from agent_tools.chair_plan_recover import plan_recover

# An older run's record says approved, but the store has the task back at ready, so it is in no approved list.
STRANDED = [{"run": "i-1", "task": "old", "phase": "p", "branch": "i/old", "remedy": None}]
STORE_APPROVED = [{"id": "other", "initiative": "i"}]


def test_a_stranded_row_for_a_task_the_store_has_back_at_ready_still_relaunches():
    quarantines = quarantine_facts([], STRANDED, [], set(), lambda initiative, task: False, approved=STORE_APPROVED)
    actions = plan_recover(_facts([_initiative()], quarantines))
    assert actions == [{"kind": "clear_branches", "initiative": "i"}, {"kind": "relaunch", "initiative": "i"}]
    assert not [a for a in actions if a["kind"] == "needs_chair" and a.get("cause") == "stranded"]


def test_the_same_row_unfiltered_blocks_the_relaunch_with_a_stranded_needs_chair():
    quarantines = quarantine_facts([], STRANDED, [], set(), lambda initiative, task: False)
    actions = plan_recover(_facts([_initiative()], quarantines))
    assert actions == [{"kind": "needs_chair", "initiative": "i", "task_id": "old", "cause": "stranded"}]
