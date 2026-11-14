from agent_tools.review_pr_close import (
    closable_prs,
    close_comment,
    review_branch,
    review_prs_for_task,
    swept_comment,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_review_branch_form():
    assert review_branch("i", "a") == "review/i/a"


def test_exact_match_selected():
    prs = [{"number": 1, "head": "review/i/a"}, {"number": 2, "head": "review/i/b"}]
    assert review_prs_for_task("i", "a", prs) == [{"number": 1, "head": "review/i/a"}]


def test_sibling_task_with_same_prefix_not_selected():
    prs = [{"number": 3, "head": "review/i/a-b"}]
    assert review_prs_for_task("i", "a", prs) == []


def test_other_initiative_not_selected():
    prs = [{"number": 4, "head": "review/j/a"}]
    assert review_prs_for_task("i", "a", prs) == []


def test_empty_list_selects_nothing():
    assert review_prs_for_task("i", "a", []) == []


def test_comment_has_full_sha_and_task():
    text = close_comment(SHA, "my-task")
    assert SHA in text
    assert "my-task" in text


CLOSED = {"done", "dropped"}


def _task(task_id, state, phase="p1", initiative="i"):
    return {"initiative": initiative, "phase": phase, "task_id": task_id, "state": state}


def test_task_id_head_with_done_task_closes():
    prs = [{"number": 7, "head": "review/i/a"}]
    assert closable_prs(prs, [_task("a", "done")], CLOSED) == [(7, "review/i/a", ("a",))]


def test_phase_head_with_one_open_task_left_alone():
    prs = [{"number": 8, "head": "review/i/p1"}]
    rows = [_task("a", "done"), _task("b", "ready")]
    assert closable_prs(prs, rows, CLOSED) == []


def test_phase_head_with_all_tasks_done_or_removed_closes():
    prs = [{"number": 9, "head": "review/i/p1"}]
    rows = [_task("a", "done"), _task("b", "dropped")]
    assert closable_prs(prs, rows, CLOSED) == [(9, "review/i/p1", ("a", "b"))]


def test_unknown_subject_left_alone():
    prs = [{"number": 10, "head": "review/i/zzz"}]
    assert closable_prs(prs, [_task("a", "done")], CLOSED) == []


def test_head_from_other_initiative_left_alone():
    prs = [{"number": 11, "head": "review/j/a"}]
    assert closable_prs(prs, [_task("a", "done")], CLOSED) == []


def test_malformed_row_skipped():
    prs = [{"head": "review/i/a"}, {"number": "12", "head": "review/i/a"}, {"number": 13, "head": "review/i/a"}]
    assert closable_prs(prs, [_task("a", "done")], CLOSED) == [(13, "review/i/a", ("a",))]


def test_swept_comment_names_tasks_on_one_line():
    text = swept_comment(["a", "b"])
    assert "a, b" in text
    assert "\n" not in text
