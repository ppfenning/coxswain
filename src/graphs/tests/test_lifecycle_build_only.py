"""The lifecycle graph's build-only mode, the phase lane's task build: plan and build, never a review of any kind; and
the scripted runner's per-task answers, which let parallel tasks each get their own build."""

from graphs.delivery import lifecycle_propose
from harness import phase_pick
from runner import ScriptedRunner


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
    approved = phase_pick.lane_verdict(["t1"], [], (2, "approve"), 1)
    assert (approved.kind, approved.outcome) == ("ack", "approved")
    assert phase_pick.lane_verdict(["t1"], ["t1"], (1, "quarantine"), 1).outcome == "quarantined"
    waiting = phase_pick.lane_verdict(["t1", "t2"], ["t1", "t2"], None, 2)
    assert (waiting.kind, waiting.refund, waiting.wait_s) == ("retry", True, phase_pick.REVIEW_WAIT_S)
    left = phase_pick.lane_verdict(["t1", "t2"], ["t1"], None, 1)
    assert (left.kind, left.refund, left.reason) == ("retry", False, "tasks not rolled up: t2")
    spent = phase_pick.lane_verdict(["t1", "t2"], ["t1"], None, phase_pick.MAX_ATTEMPTS)
    assert (spent.kind, spent.outcome) == ("quarantine", "failed")
    assert phase_pick.lane_verdict([], [], None, 1).reason == "the phase has no tasks in the store"
