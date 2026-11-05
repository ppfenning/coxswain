from agent_tools.chair_read_idle_backlog import blocked_ready, idle_backlog
from agent_tools.chair_runnable import Blocking, Runnable, TaskView, runnable_verdict

PHASES = ("p1", "p2")
TASKS = (TaskView("a", "p1", "approved"), TaskView("b", "p2", "ready", ("a",)))
ITEMS = [
    {"id": "a", "initiative": "i", "phase": "p1", "state": "approved", "needs": []},
    {"id": "b", "initiative": "i", "phase": "p2", "state": "todo", "needs": ["a"]},
]


def test_an_approved_need_is_met_at_ticket():
    assert runnable_verdict(TASKS, PHASES, land_at="ticket") == Runnable(("b",))


def test_an_approved_need_is_met_at_level():
    assert runnable_verdict(TASKS, PHASES, land_at="level") == Runnable(("b",))


def test_an_approved_need_still_blocks_at_task():
    assert runnable_verdict(TASKS, PHASES, land_at="task") == Blocking("p1", 0, 1)


def test_a_quarantined_task_still_blocks_at_ticket_beside_an_approved_one():
    tasks = (*TASKS, TaskView("q", "p1", "quarantined"))
    assert runnable_verdict(tasks, PHASES, land_at="ticket") == Blocking("p1", 1, 0)


def test_blocked_ready_is_empty_at_ticket_and_level():
    assert blocked_ready(ITEMS, {"i": "ticket"}) == []
    assert blocked_ready(ITEMS, {"i": "level"}) == []


def test_a_missing_initiative_reads_as_ticket():
    assert blocked_ready(ITEMS) == []


def test_blocked_ready_lists_the_entry_at_task():
    assert blocked_ready(ITEMS, {"i": "task"}) == [{"task": "b", "unlanded_needs": ["a"]}]


def test_idle_backlog_threads_land_at():
    plain = {
        "docket": None,
        "items": ITEMS,
        "queued": [],
        "last_launch": None,
        "last_land": None,
        "node_calls": [],
        "with_file": [],
        "decomposed": {},
        "live": [],
    }
    assert idle_backlog({**plain, "land_at": {"i": "task"}})["blocked_ready"] == [
        {"task": "b", "unlanded_needs": ["a"]}
    ]
    assert idle_backlog({**plain, "land_at": {"i": "ticket"}})["blocked_ready"] == []
