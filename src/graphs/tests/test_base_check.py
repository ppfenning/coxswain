from harness.base_check import Expected, expected_merges, gaps, stale_base_stop


def _item(id, phase="p1", needs=(), state="done", merged=True, merged_commit="c"):
    return {
        "id": id,
        "phase": phase,
        "needs": list(needs),
        "state": state,
        "merged": merged,
        "merged_commit": merged_commit,
    }


def test_an_approved_and_merged_need_is_expected():
    items = [_item("a", state="approved", merged_commit="ca"), _item("b", phase="p2", needs=["a"], state="todo")]
    assert expected_merges(items, "p2", ["b"], []) == (Expected("a", "ca", "need"),)


def test_a_done_need_with_a_commit_is_expected():
    items = [_item("a", merged_commit="ca"), _item("b", phase="p2", needs=["a"], state="todo")]
    assert expected_merges(items, "p2", ["b"], []) == (Expected("a", "ca", "need"),)


def test_an_approved_need_not_merged_is_not_expected():
    items = [_item("a", state="approved", merged=False), _item("b", needs=["a"])]
    assert expected_merges(items, "p2", ["b"], []) == ()


def test_a_todo_need_is_not_expected():
    items = [_item("a", state="todo"), _item("b", needs=["a"])]
    assert expected_merges(items, "p2", ["b"], []) == ()


def test_a_dropped_need_is_not_expected():
    items = [_item("a", state="dropped"), _item("b", needs=["a"])]
    assert expected_merges(items, "p2", ["b"], []) == ()


def test_a_need_without_a_commit_is_not_expected():
    items = [_item("a", merged_commit=""), _item("b", needs=["a"])]
    assert expected_merges(items, "p2", ["b"], []) == ()


def test_a_parent_phase_contributes_its_merged_tasks():
    items = [_item("a", phase="p1", merged_commit="ca"), _item("b", phase="p1", state="todo"), _item("c", phase="p2")]
    assert expected_merges(items, "p2", ["c"], ["p1"]) == (Expected("a", "ca", "parent"),)


def test_a_need_in_another_initiative_is_skipped():
    assert expected_merges([_item("b", needs=["other-x"])], "p2", ["b"], []) == ()


def test_a_need_also_in_a_parent_phase_appears_once_as_need():
    items = [_item("a", phase="p1", merged_commit="ca"), _item("b", phase="p2", needs=["a"])]
    assert expected_merges(items, "p2", ["b"], ["p1"]) == (Expected("a", "ca", "need"),)


def test_expected_merges_is_sorted_by_task_id():
    items = [_item("z", phase="p1"), _item("m", phase="p1"), _item("b", phase="p2", needs=["z", "m"])]
    assert [e.task for e in expected_merges(items, "p2", ["b"], ["p1"])] == ["m", "z"]


def test_gaps_returns_only_the_absent_ones_in_order():
    exp = (Expected("a", "ca", "need"), Expected("b", "cb", "need"), Expected("c", "cc", "parent"))
    assert gaps(exp, {"a": True, "b": False}) == (exp[1], exp[2])


def test_stale_base_stop_names_every_missing_task():
    miss = (Expected("z", "cz", "need"), Expected("a", "ca", "parent"))
    assert stale_base_stop(miss) == "z: the build base lacks the merged work of a, z and could not be rebuilt"


def test_stale_base_stop_is_empty_for_no_gaps():
    assert stale_base_stop(()) == ""
