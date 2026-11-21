"""The phase lane's schedule and its progress, pure: example inputs, no git and no store."""

from harness import lane_plan, lane_progress, lane_rollup, phase_lane
from harness.lane_progress import Next

DAG = {"t1": (), "t2": (), "t3": ("t1", "t2")}


def test_independent_tasks_start_together_and_a_dependent_waits_for_both_roll_ups():
    assert lane_plan.next_tasks(DAG, set(), set(), 4) == ["t1", "t2"]
    assert lane_plan.next_tasks(DAG, {"t1"}, {"t2"}, 4) == []  # t2 is building, not rolled up: t3 waits
    assert lane_plan.next_tasks(DAG, {"t1", "t2"}, set(), 4) == ["t3"]
    assert lane_plan.next_tasks(DAG, {"t1", "t2", "t3"}, set(), 4) == []


def test_no_more_start_than_the_threads_free_and_none_without_a_thread():
    assert lane_plan.next_tasks(DAG, set(), set(), 1) == ["t1"]
    assert lane_plan.next_tasks(DAG, set(), set(), 0) == []


def test_a_need_outside_the_phase_is_an_earlier_phase_and_never_waits():
    assert lane_plan.next_tasks({"t4": ("p0-task",)}, set(), set(), 2) == ["t4"]


def test_a_failed_task_is_not_restarted_and_what_needs_it_is_blocked():
    dag = {**DAG, "t4": ("t3",), "t5": ()}
    assert lane_plan.next_tasks(dag, {"t2"}, set(), 4, failed={"t1"}) == ["t5"]
    assert lane_plan.blocked(dag, {"t1"}) == ["t3", "t4"]
    assert lane_plan.blocked(dag, set()) == []


def test_the_thread_budget_is_the_hosts_row_or_the_cores_split_among_the_lanes():
    assert lane_plan.threads(0, 16, 2) == 8
    assert lane_plan.threads(6, 16, 2) == 3
    assert lane_plan.threads(0, 2, 4) == 1


def _record(sha, initiative="ship", phase="p1", task="", kind="task", round_=""):
    return "\x1f".join([sha, f"{initiative}\n", f"{phase}\n", f"{task}\n" if task else "", f"{kind}\n", round_]) + "\x1e\n"


def test_the_log_gives_each_tasks_commit_and_sets_aside_what_is_not_this_phases():
    log = (_record("a1", task="t1") + _record("b2", phase="p2", task="t9") + _record("c3")
           + _record("d4", task="t2") + _record("e5", task="p1-rev-1", kind="revision", round_="1\n"))
    progress = lane_progress.read(log, "ship", "p1")
    assert progress.done == {"t1": "a1", "t2": "d4", "p1-rev-1": "e5"}
    assert progress.revisions == (1,)
    assert progress.ignored == ("b2", "c3")


def test_resume_builds_what_is_missing_then_goes_to_review_and_a_round_decides():
    tasks = ["t1", "t2", "t3"]
    assert lane_progress.resume_plan(tasks, [], None) == Next("build", ("t1", "t2", "t3"))
    assert lane_progress.resume_plan(tasks, ["t1", "t2"], None) == Next("build", ("t3",))
    assert lane_progress.resume_plan(tasks, tasks, None) == Next("review", round=1)
    assert lane_progress.resume_plan(tasks, tasks, (1, "revise")) == Next("build_revision", round=1)
    assert lane_progress.resume_plan(tasks, tasks, (1, "revise"), [1]) == Next("review", round=2)
    assert lane_progress.resume_plan(tasks, [], (2, "approve")) == Next("finish", outcome="approved")
    assert lane_progress.resume_plan(tasks, [], (1, "quarantine")) == Next("finish", outcome="quarantined")


def test_the_roll_up_message_keeps_todays_subject_and_carries_the_trailers():
    text = lane_rollup.message("ship", "p1", "t3", "ship.p1-2", "Add  the\nnote")
    assert text.splitlines()[:3] == ["epic ship.p1-2: t3", "", "Add the note"]
    assert text.splitlines()[4:] == ["Cox-Initiative: ship", "Cox-Phase: p1", "Cox-Task: t3", "Cox-Run: ship.p1-2",
                                     "Cox-Kind: task"]
    revision = lane_rollup.message("ship", "p1", "p1-rev-2", "r", "", kind="revision", round_=2, merged_with="t1")
    assert revision.splitlines()[-2:] == ["Cox-Round: 2", "Cox-Merged-With: t1"]
    assert lane_rollup.branch_name("ship", "p1") == "epic/ship/p1"


def test_the_lane_builds_the_phases_open_tasks_and_reads_their_needs():
    items = [{"id": "t2", "phase": "p1", "state": "ready", "needs": ["t1"]}, {"id": "t1", "phase": "p1", "state": "approved"},
             {"id": "t0", "phase": "p1", "state": "dropped"}, {"id": "t9", "phase": "p2", "state": "ready"}]
    tasks = phase_lane.phase_tasks(items, "p1")
    assert [t["id"] for t in tasks] == ["t1", "t2"]
    assert phase_lane.task_dag(tasks) == {"t1": (), "t2": ("t1",)}


def test_the_base_is_the_first_of_the_row_the_record_the_initiative_branch_and_the_target():
    assert phase_lane.base_candidates("", "", "ship", "origin/main") == ["init/ship", "main"]
    assert phase_lane.base_candidates("init/x", "init/x", "ship", "origin/main") == ["init/x", "init/ship", "main"]


def test_the_lane_writes_as_the_workers_role_only_when_its_environment_names_one():
    joined = phase_lane.lane_url("postgresql://own@h/db", "cox_w_a", "pw", lambda u, r, p: f"{u}|{r}|{p}")
    assert joined == ("postgresql://own@h/db|cox_w_a|pw", "cox_w_a")
    assert phase_lane.lane_url("postgresql://own@h/db", "cox_w_a", "", lambda *_: "x")[0] == "postgresql://own@h/db"
    assert phase_lane.lane_url("sqlite:///s.db", "cox_w_a", "pw", lambda *_: "x")[0] == "sqlite:///s.db"


def test_a_commit_on_the_branch_with_no_rollup_row_is_written_again_and_only_that_one():
    stored = [{"phase": "p1", "task_id": "t1", "subject_sha": "a1"}, {"phase": "p2", "task_id": "t2", "subject_sha": "b2"}]
    assert phase_lane.missing_rollups({"t1": "a1", "t2": "b2"}, stored, "p1") == {"t2": "b2"}


def test_the_exit_says_lost_before_failed_before_built():
    assert phase_lane.exit_code(True, ["t1"]) == phase_lane.EXIT_LOST
    assert phase_lane.exit_code(False, ["t1"]) == phase_lane.EXIT_FAILED
    assert phase_lane.exit_code(False, []) == phase_lane.EXIT_BUILT
    assert phase_lane.lanes_of({"COX_LANE_CAPACITY": "3"}) == 3 and phase_lane.lanes_of({}) == 1


def test_a_push_that_did_not_land_is_reconciled_and_only_a_lost_claim_stops_the_lane():
    move = lane_rollup.next_move
    assert move(False, "o", "ours", "tip", False, 1) == "lost"
    assert move(True, "ours", "ours", "tip", True, 1) == "done"  # it arrived though the answer did not
    assert move(True, "theirs", "ours", "tip", True, 1) == "adopt"  # a stale lane landed this task
    assert move(True, "moved", "ours", "tip", False, 1) == "reapply"  # another task landed meanwhile
    assert move(True, "tip", "ours", "tip", False, 1) == "retry"  # origin did not move: the network failed
    assert move(True, None, "ours", "tip", False, 2) == "retry"  # nor could origin be read
    assert move(True, "moved", "ours", "tip", False, 3) == "fail"  # past three tries the task fails alone
    assert move(True, "tip", "ours", "tip", False, 3) == "fail"


def test_a_push_is_rejected_only_when_origin_moved_and_a_killed_push_is_an_error():
    assert lane_rollup.push_outcome(0, "") == "pushed"
    assert lane_rollup.push_outcome(1, "!\tHEAD:refs/heads/x\t[rejected] (fetch first)") == "rejected"
    assert lane_rollup.push_outcome(128, "fatal: unable to access: Could not resolve host") == "error"
    assert lane_rollup.push_outcome(None, "[rejected]") == "error"  # killed for running over: nothing is known


def test_a_new_lane_sweeps_only_the_phases_other_runs():
    names = ["ship.p1-1", "ship.p1-2", "ship.p1-3", "ship.p10-1", "ship.p2-1", "other"]
    assert lane_rollup.stale_runs(names, "ship.p1", "ship.p1-3") == ["ship.p1-1", "ship.p1-2"]


def test_the_lanes_progress_mark_sits_beside_its_pidfile(tmp_path):
    path = phase_lane.beat_path(tmp_path, "ship.p1-2")
    phase_lane.write_beat(path, 7)
    assert path == tmp_path / "ship.p1-2.lane-beat" and path.read_text() == "7\n"
