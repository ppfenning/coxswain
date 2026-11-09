from agent_tools import intake_queue


def _row(task_id, extra):
    return {
        "kind": "intake",
        "initiative": "intake",
        "task_id": task_id,
        "phase": "",
        "state": "queued",
        "needs": [],
        "title": task_id,
        "surfaces": [],
        "body": "",
        "extra": extra,
    }


def test_absent_stage_is_ready():
    assert intake_queue.stage({}) == "ready"


def test_unrecognised_stage_is_ready():
    assert intake_queue.stage({"stage": "later"}) == "ready"


def test_stage_draft_is_draft():
    assert intake_queue.stage({"stage": "draft"}) == "draft"


def test_held_true_is_draft():
    assert intake_queue.stage({"held": True}) == "draft"


def test_quoted_draft_is_draft():
    assert intake_queue.stage({"stage": "'draft'"}) == "draft"


def test_runnable_intake_leaves_out_a_draft_and_keeps_a_ready_row():
    draft = _row("a", {"stage": "draft"})
    ready = _row("b", {"stage": "ready"})
    assert intake_queue.runnable_intake([draft, ready]) == [ready]


def test_held_rows_lists_a_draft():
    draft = _row("a", {"stage": "draft"})
    ready = _row("b", {})
    assert intake_queue.held_rows([draft, ready]) == [draft]


def test_higher_priority_ready_row_sorts_first():
    low = _row("low", {"priority": 1})
    high = _row("high", {"priority": 5})
    assert intake_queue.runnable_intake([low, high]) == [high, low]
