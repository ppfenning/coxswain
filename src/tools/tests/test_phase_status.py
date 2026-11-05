from agent_tools.phase_status import phase_status


def test_all_settled_and_validated_is_done():
    assert phase_status(["done", "dropped"], True) == "done"


def test_one_unsettled_task_is_open():
    assert phase_status(["done", "ready"], True) == "open"


def test_not_validated_is_open():
    assert phase_status(["done", "done"], False) == "open"


def test_empty_list_is_open():
    assert phase_status([], True) == "open"


def test_approved_counts_as_settled():
    assert phase_status(["approved", "done"], True) == "done"
