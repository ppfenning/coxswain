"""`lane_merge`'s pure core: what a collision comes to, the row it leaves, which task it is with, and what counts as a
merge. The edge (`resolve`, and the lane around it) is proven end to end in `smoke/test_lane_merge.py`."""

import pytest

from harness import lane_merge

T1 = ("a" * 40, "t1", ("src/app.py", "notes/t1.txt"))
T3 = ("b" * 40, "t3", ("docs/x.md",))
T2 = ("c" * 40, "t2", ("src/app.py",))


@pytest.mark.parametrize(("merge_ok", "a_ok", "b_ok", "want"), [
    (True, True, True, "commit"),
    (False, True, True, "rebuild"),
    (True, False, True, "rebuild"),
    (True, True, False, "rebuild"),
    (False, False, False, "rebuild"),
])
def test_only_a_resolved_merge_passing_both_tasks_checks_is_committed(merge_ok, a_ok, b_ok, want):
    assert lane_merge.outcome(merge_ok, a_ok, b_ok) == want


def test_the_collision_row_names_both_tasks_and_each_shared_file_once_sorted():
    row = lane_merge.collision_row("t2", "t1", ["src/b.py", "src/a.py", "src/b.py"])
    assert row == {"task": "t2", "other": "t1", "files": ["src/a.py", "src/b.py"]}


def test_landed_reads_each_commit_its_task_and_paths_from_the_log_format_it_names():
    text = (f"\x1e{T1[0]}\x1ft1\x1f\nsrc/app.py\nnotes/t1.txt\n"
            f"\x1e{'d' * 40}\x1f\x1f\nREADME.md\n")
    assert lane_merge.landed(text) == [T1, ("d" * 40, "", ("README.md",))]
    assert lane_merge.landed("") == []


def test_every_landed_task_that_touched_a_shared_file_collides_oldest_first():
    commits = [T1, T3, T2]
    assert lane_merge.shared_files(["src/app.py", "src/new.py"], commits) == ["src/app.py"]
    assert lane_merge.colliding(["src/app.py"], commits) == [("t1", "a" * 40), ("t2", "c" * 40)]
    assert lane_merge.colliding(["notes/t1.txt"], commits) == [("t1", "a" * 40)]
    assert lane_merge.colliding(["elsewhere.py"], commits) == []
    assert lane_merge.colliding(["README.md"], [("d" * 40, "", ("README.md",))]) == []  # not a task commit


@pytest.mark.parametrize(("checks", "verify", "want"), [
    ([{"name": "unit", "passed": True}], [], True),
    ([{"name": "unit", "passed": True}], [{"name": "verify-1", "passed": True}], True),
    ([], [], False),  # no check ran: no evidence, so not a pass
    ([], [{"name": "verify-1", "passed": True}], False),  # verify alone is not the task's cheap checks
    ([{"name": "unit", "passed": False}], [], False),
    ([{"name": "unit", "passed": True}], [{"name": "verify-1", "passed": False}], False),
    ([{"name": "unit"}], [], False),  # a result that does not say it passed did not
])
def test_a_side_passes_only_when_some_check_ran_and_every_check_and_verify_passed(checks, verify, want):
    assert lane_merge.side_ok(checks, verify) is want


def test_a_side_with_no_checks_makes_the_merge_a_rebuild():
    assert lane_merge.outcome(True, lane_merge.side_ok([], []), lane_merge.side_ok([{"passed": True}], [])) == "rebuild"


def test_conflict_markers_are_found_only_at_a_line_start():
    assert lane_merge.has_markers("a\n<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n")
    assert lane_merge.has_markers("a\n<<<<<<<\n")
    assert lane_merge.has_markers("a\n||||||| base\nx\n")  # diff3 style: the common ancestor's section
    assert lane_merge.has_markers("a\n|||||||\n")
    assert not lane_merge.has_markers("a\n=======\nb\n")  # a heading underline is not a marker
    assert not lane_merge.has_markers("x = '<<<<<<< not at the start'\n")
    assert not lane_merge.has_markers("x = a ||||||||| b\n")


BASE = "def greet(name):\n    return name\n"
OURS = "def greet(name):\n    name = name.strip()\n    return name\n"
THEIRS = "def greet(name):\n    name = name.title()\n    return name\n"


def test_a_merge_keeping_both_sides_added_lines_loses_none():
    merged = "def greet(name):\n    name = name.strip()\n    name = name.title()\n    return name\n"
    assert lane_merge.lost_lines(BASE, OURS, THEIRS, merged) == []


def test_a_merge_that_drops_one_sides_line_names_it():
    assert lane_merge.lost_lines(BASE, OURS, THEIRS, OURS) == ["    name = name.title()"]
    assert lane_merge.lost_lines(BASE, OURS, THEIRS, THEIRS) == ["    name = name.strip()"]


def test_a_dropped_addition_of_a_line_the_base_already_holds_is_still_lost():
    """The base calls `flush()` in `a()`; theirs adds a `flush()` to `b()`; the merge drops it. The merged file still
    holds the base's `flush()`, which must not stand in for the added one."""
    base = "def a():\n    flush()\n\n\ndef b():\n    pass\n"
    theirs = "def a():\n    flush()\n\n\ndef b():\n    flush()\n    pass\n"
    ours = "def a():\n    flush()\n    log()\n\n\ndef b():\n    pass\n"
    merged = "def a():\n    flush()\n    log()\n\n\ndef b():\n    pass\n"
    assert lane_merge.lost_lines(base, ours, theirs, merged) == ["    flush()"]


def test_a_line_moved_to_another_indent_is_lost():
    """`return 0` added under the `if` and moved out from under it by the merge: its meaning changed."""
    base = "def f(x):\n    if x:\n        pass\n    return 1\n"
    theirs = "def f(x):\n    if x:\n        return 0\n    return 1\n"
    merged = "def f(x):\n    if x:\n        pass\n    return 0\n    return 1\n"
    assert lane_merge.lost_lines(base, base, theirs, merged) == ["        return 0"]


def test_trailing_whitespace_alone_is_not_a_loss():
    assert lane_merge.lost_lines(BASE, OURS, BASE, OURS.replace("strip()\n", "strip()   \n")) == []


def test_a_line_both_sides_added_need_appear_once_and_a_line_added_twice_twice():
    twice = "def greet(name):\n    log()\n    log()\n    return name\n"
    assert lane_merge.lost_lines(BASE, OURS, OURS, OURS) == []
    assert lane_merge.lost_lines(BASE, twice, BASE, "def greet(name):\n    log()\n    return name\n") == ["    log()"]
    assert lane_merge.lost_lines("", "a\n", "b\n", "a\n") == ["b"]  # a file both sides added: no base


def test_a_resolution_is_each_conflicted_file_merged_and_nothing_else():
    good = {"resolved": True, "summary": "kept both", "files": [{"path": "src/app.py", "content": "both\n"}]}
    assert lane_merge.resolution(good, ["src/app.py"]) == {"src/app.py": "both\n"}


@pytest.mark.parametrize(("answer", "says"), [
    ({"resolved": False, "summary": "they contradict", "files": []}, "did not resolve it: they contradict"),
    ({"resolved": True, "summary": "", "files": []}, "left out src/app.py"),
    ({"resolved": True, "summary": "", "files": [{"path": "src/app.py", "content": "x\n"},
                                                  {"path": "other.py", "content": "y\n"}]}, "do not conflict: other.py"),
    ({"resolved": True, "summary": "", "files": [{"path": "src/app.py", "content": "<<<<<<< ours\n"}]},
     "conflict markers in src/app.py"),
    ({"resolved": "yes", "files": "src/app.py"}, "did not resolve it"),
])
def test_an_answer_that_is_not_a_merge_says_why(answer, says):
    why = lane_merge.resolution(answer, ["src/app.py"])
    assert isinstance(why, str) and says in why


def test_the_brief_carries_both_tasks_the_change_and_each_conflicted_file():
    text = lane_merge.brief("t2", "Add b", "t1", "Add a", {"src/app.py": "<<<<<<< ours\na\n=======\nb\n>>>>>>> t2\n"},
                            "diff --git a/src/app.py b/src/app.py\n")
    assert "`t1` (Add a)" in text and "`t2` (Add b)" in text
    assert '<file path="src/app.py">' in text and "diff --git a/src/app.py" in text
    assert "resolved: false" in text
