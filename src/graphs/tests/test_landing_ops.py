"""The phase merge over a real repo and a real store: the record, the queue item and the branch move together."""
import pytest

from core import landing_record as lr
from harness import landing_git as lg
from harness import landing_ops as ops
from harness import landing_store as ls
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate
from tests.test_landing_git import commit, cut, repo, run  # noqa: F401 -- repo is a fixture

T0 = "2026-11-12T10:00:00Z"
T1 = "2026-11-12T10:01:00Z"
T9 = "2026-11-12T12:00:00Z"


@pytest.fixture
def conn(store_url):
    c = connect(store_url)
    migrate(c, T0, default_modules())
    yield c
    c.close()


def task(ident, phase, state="approved", needs=()):
    return {"id": ident, "phase": phase, "state": state, "needs": list(needs)}


def start(conn, repo):  # noqa: F811
    record, why = ops.start_record(conn, repo, initiative="i", branch="init/i", parent="main", parent_ref="main", now=T0)
    assert why == "" and record is not None
    return record


def build(repo, phase, name=None, text=None):  # noqa: F811
    cut(repo, f"epic/i/{phase}", "init/i")
    return commit(repo, f"epic/i/{phase}", name or f"{phase}.txt", text or f"{phase}\n")


def test_the_record_is_made_once_with_the_branch_cut_at_the_parent_and_the_parent_no_longer_floats(conn, repo):  # noqa: F811
    base = run(repo, "rev-parse", "main")
    record = start(conn, repo)
    assert (record["branch"], record["parent"], record["base_sha"], record["state"]) == ("init/i", "main", base, "building")
    assert run(repo, "rev-parse", "init/i") == base
    commit(repo, "main", "moved.txt", "moved\n")
    again, _ = ops.start_record(conn, repo, initiative="i", branch="init/other", parent="develop", parent_ref="main", now=T1)
    assert (again["branch"], again["parent"], again["base_sha"]) == ("init/i", "main", base)


def test_a_built_phase_merges_and_the_record_the_queue_and_the_branch_agree(conn, repo):  # noqa: F811
    start(conn, repo)
    p1_tip = build(repo, "p1")
    tasks = [task("a", "p1"), task("b", "p2", "ready", needs=("a",))]
    done = ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=tasks, worker="run-1", now=T1)
    assert (done.status, done.tip, done.merged_tasks, done.assembled) == ("merged", p1_tip, ("a",), False)
    assert run(repo, "rev-parse", "init/i") == done.merge_sha
    record = ls.get_record(conn, "i")
    assert record["phases"]["p1"]["state"] == "merged"
    assert (record["phases"]["p1"]["tip"], record["phases"]["p1"]["merge_sha"]) == (p1_tip, done.merge_sha)
    assert record["waves"] == {"1": {"green_tip": done.merge_sha, "phases": ["p1"]}}
    assert ls.get_item(conn, "phase_merge:i:p1")["state"] == "done"
    assert lr.placeable_phases(record, tasks) == ["p2"], "wave 2 opens only now"


def test_merging_the_last_phase_assembles_the_initiative(conn, repo):  # noqa: F811
    start(conn, repo)
    build(repo, "p1")
    tasks = [task("a", "p1")]
    done = ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=tasks, worker="run-1", now=T1)
    assert done.assembled is True
    assert ls.get_record(conn, "i")["state"] == "assembled"


def test_a_phase_that_is_not_built_or_whose_parent_is_unmerged_is_not_ready(conn, repo):  # noqa: F811
    start(conn, repo)
    build(repo, "p1")
    half = [task("a", "p1"), task("a2", "p1", "ready")]
    assert ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=half, worker="w", now=T1).status == "not_ready"
    child = [task("a", "p1"), task("b", "p2", needs=("a",))]
    assert ops.merge_phase(conn, repo, initiative="i", phase="p2", tasks=child, worker="w", now=T1).status == "not_ready"
    assert ls.list_items(conn) == []


def test_merging_a_merged_phase_again_changes_nothing(conn, repo):  # noqa: F811
    start(conn, repo)
    build(repo, "p1")
    tasks = [task("a", "p1")]
    first = ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=tasks, worker="w", now=T1)
    again = ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=tasks, worker="w2", now=T9)
    assert (again.status, again.merge_sha, again.assembled) == ("already_merged", first.merge_sha, True)
    assert run(repo, "rev-parse", "init/i") == first.merge_sha


def test_a_second_worker_finds_the_phase_busy_and_a_dead_workers_claim_is_taken_over_once(conn, repo):  # noqa: F811
    start(conn, repo)
    build(repo, "p1")
    tasks = [task("a", "p1")]
    ls.enqueue(conn, ls.PHASE_MERGE, "i", phase="p1", repo=str(repo), now=T0)
    assert ls.claim(conn, "phase_merge:i:p1", "dead-run", now=T0, lease_until=ops.later(T0, 900))  # then it died
    busy = ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=tasks, worker="lander", now=T1)
    assert (busy.status, busy.detail) == ("busy", "dead-run is merging phase p1")
    taken = ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=tasks, worker="lander", now=T9)
    assert taken.status == "merged"
    item = ls.get_item(conn, "phase_merge:i:p1")
    assert (item["state"], item["claimed_by"], item["attempts"]) == ("done", "lander", 2)


def test_two_phases_editing_one_line_the_second_is_conflicted_with_the_paths_and_nothing_is_lost(conn, repo):  # noqa: F811
    start(conn, repo)
    build(repo, "p1", "shared.txt", "one\nTWO from p1\nthree\n")
    p2_tip = build(repo, "p2", "shared.txt", "one\nTWO from p2\nthree\n")
    tasks = [task("a", "p1"), task("b", "p2")]
    assert ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=tasks, worker="w", now=T1).ok
    before = run(repo, "rev-parse", "init/i")
    clash = ops.merge_phase(conn, repo, initiative="i", phase="p2", tasks=tasks, worker="w", now=T1)
    assert (clash.status, clash.cause, clash.paths) == ("conflicted", "conflict", ("shared.txt",))
    assert run(repo, "rev-parse", "init/i") == before
    assert run(repo, "rev-parse", "epic/i/p2") == p2_tip, "the phase branch still holds its approved work"
    record = ls.get_record(conn, "i")
    assert record["phases"]["p2"]["state"] == "conflicted" and record["phases"]["p2"]["conflict_paths"] == ["shared.txt"]
    item = ls.get_item(conn, "phase_merge:i:p2")
    assert (item["state"], item["last_error"]) == ("failed", "conflict in shared.txt")
    assert lr.initiative_state(record, [*tasks, task("b-rectify", "p2", "ready")]) == "conflicted"


def test_red_level_checks_mark_the_phase_conflicted_with_the_output_and_leave_the_branch(conn, repo):  # noqa: F811
    start(conn, repo)
    build(repo, "p1")
    before = run(repo, "rev-parse", "init/i")
    red = ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=[task("a", "p1")], worker="w", now=T1,
                          checks=lambda tree: (False, "tests/test_x.py::test_y failed"))
    assert (red.status, red.cause, red.detail) == ("conflicted", "checks_failed", "tests/test_x.py::test_y failed")
    assert run(repo, "rev-parse", "init/i") == before
    assert ls.get_record(conn, "i")["phases"]["p1"]["cause"] == "checks_failed"


def test_a_rectified_phase_merges_on_the_next_try(conn, repo):  # noqa: F811
    start(conn, repo)
    build(repo, "p1", "shared.txt", "one\nTWO from p1\nthree\n")
    build(repo, "p2", "shared.txt", "one\nTWO from p2\nthree\n")
    tasks = [task("a", "p1"), task("b", "p2")]
    ops.merge_phase(conn, repo, initiative="i", phase="p1", tasks=tasks, worker="w", now=T1)
    ops.merge_phase(conn, repo, initiative="i", phase="p2", tasks=tasks, worker="w", now=T1)
    lg.conflict_commit(repo, branch="epic/i/p2", source="init/i", message="merge init/i into p2 (conflicted)")
    commit(repo, "epic/i/p2", "shared.txt", "one\nTWO from both\nthree\n")  # the rectify task's approved patch
    rectified = [*tasks, task("b-rectify", "p2")]
    healed = ops.merge_phase(conn, repo, initiative="i", phase="p2", tasks=rectified, worker="w", now=T9)
    assert (healed.status, healed.assembled) == ("merged", True)
    assert run(repo, "show", "init/i:shared.txt") == "one\nTWO from both\nthree"
    assert ls.get_item(conn, "phase_merge:i:p2")["state"] == "done"
