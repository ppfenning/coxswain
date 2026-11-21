"""The lifecycle graph's build-only mode, the phase lane's task build: plan and build, never a review of any kind; and
the scripted runner's per-task answers, which let parallel tasks each get their own build."""

from graphs.delivery import lifecycle_propose
from harness import phase_pick
from runner import ScriptedRunner

PASSED = phase_pick.SmokeSeen(True, "")  # the ended lane's smoke, as `worker.lane_facts` reads it: passed


def _args(cartridge, ticket="t1", **extra):
    return {"run_id": f"run-1:{ticket}", "date": "2026-11-20", "ticket": ticket, "cartridge": cartridge,
            "work_item": True, "build_only": True, **extra}


def test_a_build_only_run_plans_and_builds_and_calls_no_reviewer(cartridge, plan_response, build_response):
    # No reviewer is scripted: a review, adversary or arbitration call would raise, failing the run.
    runner = ScriptedRunner({"plan": plan_response, "build": build_response})
    result = lifecycle_propose.run(_args(cartridge), runner)
    assert result["build_only"] is True and result["build_stopped"] is None
    assert result["build"]["patch"] == build_response["patch"] and result["proposals"] == []
    assert [c["role"] for c in runner.calls] == ["plan", "build"]


def test_parallel_tasks_each_get_their_own_build_answer_whatever_order_they_ask_in(cartridge, plan_response, build_response):
    other = {**build_response, "summary": "the second task's own"}
    runner = ScriptedRunner({"plan": plan_response, "build": build_response, "build@t2": other})
    second = lifecycle_propose.run(_args(cartridge, "t2"), runner)
    first = lifecycle_propose.run(_args(cartridge, "t1"), runner)
    assert (first["build"]["summary"], second["build"]["summary"]) == (build_response["summary"], other["summary"])


def test_a_lane_verdict_comes_from_the_review_round_then_from_the_roll_ups():
    approved = phase_pick.lane_verdict(["t1"], [], (2, "approve"), 1, smoke=None)
    assert (approved.kind, approved.outcome) == ("ack", "approved")
    quarantined = phase_pick.lane_verdict(["t1"], ["t1"], (1, "quarantine"), 1, smoke=PASSED, reason="goal missed")
    assert (quarantined.outcome, quarantined.reason) == ("quarantined", "the phase review quarantined it in round 1: goal missed")
    # L6: nothing is parked `built`; a lane whose review loop did not finish the phase is retried, its claim counted.
    unreviewed = phase_pick.lane_verdict(["t1", "t2"], ["t1", "t2"], None, 2, smoke=PASSED)
    assert (unreviewed.kind, unreviewed.refund) == ("retry", False) and "before its review finished" in unreviewed.reason
    assert phase_pick.lane_verdict(["t1"], ["t1"], (1, "revise"), 2, smoke=PASSED).kind == "retry"
    assert phase_pick.lane_verdict(["t1"], ["t1"], (2, "reopened"), 1, smoke=PASSED).kind == "retry"  # an old episode's
    out = phase_pick.lane_verdict(["t1"], ["t1"], (1, "revise"), phase_pick.MAX_ATTEMPTS, smoke=PASSED)
    assert (out.kind, out.outcome) == ("quarantine", "failed")
    left = phase_pick.lane_verdict(["t1", "t2"], ["t1"], None, 1, smoke=PASSED)
    assert (left.kind, left.refund, left.reason) == ("retry", False, "tasks not rolled up: t2")
    spent = phase_pick.lane_verdict(["t1", "t2"], ["t1"], None, phase_pick.MAX_ATTEMPTS, smoke=PASSED)
    assert (spent.kind, spent.outcome) == ("quarantine", "failed")
    assert phase_pick.lane_verdict([], [], None, 1, smoke=PASSED).reason == "the phase has no tasks in the store"


def test_a_failed_smoke_is_never_parked_built_and_a_real_failure_finishes_the_phase_failed_at_once():
    # A retry would run the same commands on the same tip: the phase finishes failed, for the operator, on attempt 1.
    failed = phase_pick.SmokeSeen(False, "the phase smoke failed: configured check failed: wiring")
    done = phase_pick.lane_verdict(["t1", "t2"], ["t1", "t2"], None, 1, smoke=failed)
    assert (done.kind, done.outcome, done.reason) == ("quarantine", "failed", failed.reason)
    # A lane that ended before its smoke ran is not parked either: the next claim runs it.
    unrun = phase_pick.lane_verdict(["t1"], ["t1"], None, 1, smoke=None)
    assert (unrun.kind, unrun.reason) == ("retry", "every task rolled up, but the lane ended before the phase smoke ran")
    # The review round still decides over any smoke.
    assert phase_pick.lane_verdict(["t1"], ["t1"], (1, "approve"), 1, smoke=failed).kind == "ack"


def test_a_smoke_that_could_not_run_goes_back_to_the_queue_once_and_a_second_at_that_tip_finishes_it_failed():
    why = "the phase smoke failed: harness fault: check 'smoke' could not run: docker: not found"
    first = phase_pick.lane_verdict(["t1"], ["t1"], None, 1, smoke=phase_pick.SmokeSeen(False, why, faults=1))
    assert (first.kind, first.reason, first.refund, first.wait_s) == ("retry", why, False, phase_pick.backoff_s(1))
    second = phase_pick.lane_verdict(["t1"], ["t1"], None, 2, smoke=phase_pick.SmokeSeen(False, why, faults=2))
    assert (second.kind, second.outcome) == ("quarantine", "failed")
    assert second.reason == f"the phase smoke could not run 2 times at this tip, the last: {why}"
    # The attempts cap still holds over a first fault.
    capped = phase_pick.lane_verdict(["t1"], ["t1"], None, phase_pick.MAX_ATTEMPTS, smoke=phase_pick.SmokeSeen(False, why, 1))
    assert (capped.kind, capped.outcome) == ("quarantine", "failed")


def test_a_lane_whose_progress_mark_stands_still_is_stalled_and_one_that_moves_is_not():
    seen, stalled = phase_pick.lane_stalled(None, "", 100.0)
    assert (seen, stalled) == (("", 100.0), False)  # no mark yet: the clock starts at the first read
    seen, stalled = phase_pick.lane_stalled(seen, "1", 150.0)
    assert (seen, stalled) == (("1", 150.0), False)
    assert phase_pick.lane_stalled(seen, "1", 150.0 + phase_pick.LANE_STALL_S - 1)[1] is False
    assert phase_pick.lane_stalled(seen, "1", 150.0 + phase_pick.LANE_STALL_S)[1] is True
    assert phase_pick.lane_stalled(("", 0.0), "", phase_pick.LANE_STALL_S)[1] is True  # a lane that never marked
