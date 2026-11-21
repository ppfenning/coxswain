"""phase-review: one review round of a phase branch, its smoke as evidence, its verdict the phase's."""

from __future__ import annotations

from graphs.delivery import phase_review
from runner import ScriptedRunner
from runner.protocol import RunnerError

PATCH = (
    "diff --git a/lib/helper.py b/lib/helper.py\nnew file mode 100644\n--- /dev/null\n+++ b/lib/helper.py\n"
    "@@ -0,0 +1 @@\n+def helper(): ...\n"
    "diff --git a/lib/copy.py b/lib/copy.py\nnew file mode 100644\n--- /dev/null\n+++ b/lib/copy.py\n"
    "@@ -0,0 +1 @@\n+def helper(): ...\n"
)
SMOKE = {"verdict": "passed", "checks": [{"name": "smoke", "cmd": "pytest -q smoke", "passed": True, "exit_code": 0}]}
REVISE = {"verdict": "revise", "rationale": "a helper is written twice",
          "findings": [{"charter_principle": "no fat", "detail": "lib/copy.py repeats lib/helper.py", "file": "lib/copy.py"}]}
APPROVE = {"verdict": "approve", "findings": [], "rationale": "the goal is met and nothing is spare"}
REJECT = {"verdict": "reject", "findings": [], "rationale": "nothing reaches the notes: the goal is missed"}
ADVERSARY_REVISES = {"verdict": "revise", "objections": [{"claim": "c", "why_wrong": "w"}], "strongest_objection": "dead code"}
ARBITER_REVISES = {"verdict": "revise", "sided_with": "adversary", "reasoning": "the copy is dead"}


def args(cartridge, *roles: str, **overrides) -> dict:
    cartridge["skills"].update({role: f"acme-skills:{role}" for role in roles})
    return {"run_id": "ship.p1-1:p1-review-2", "date": "2026-11-20", "initiative": "ship", "phase": "p1", "round": 2,
            "goal": "Ship three notes.", "tasks": ["t1", "t2"], "patch": PATCH, "smoke": SMOKE, "cartridge": cartridge,
            "previous": [{"round": 1, "verdict": "revise", "reason": "trim it"}], **overrides}


def test_the_phase_reviewer_gets_the_goal_the_two_questions_the_diff_and_the_smoke(cartridge) -> None:
    scripted = ScriptedRunner({"review_phase@p1-review-2": APPROVE})
    result = phase_review.run(args(cartridge, "review_phase"), scripted)

    assert (result["verdict"], result["reviewer"], result["round"], result["harness_fault"]) == (
        "approve", "review_phase", 2, "")
    assert [c["role"] for c in scripted.calls] == ["review_phase"]  # no adversary bound: the reviewer alone
    prompt = scripted.calls[0]["prompt"]
    assert "THE PHASE'S GOAL, which is what you are judging against:\nShip three notes." in prompt
    assert "Does the phase meet its goal?" in prompt and "Does it carry fat?" in prompt
    assert "Round 1 said revise: trim it" in prompt and "+++ b/lib/copy.py" in prompt
    assert "$ pytest -q smoke\npassed (exit 0)" in prompt


def test_without_a_phase_reviewer_the_charter_reviewer_takes_the_seat(cartridge) -> None:
    scripted = ScriptedRunner({"review_charter": REVISE})
    result = phase_review.run(args(cartridge, "review_charter"), scripted)
    assert (result["verdict"], result["reviewer"], result["reason"]) == ("revise", "review_charter", "a helper is written twice")
    assert result["review"]["findings"][0]["file"] == "lib/copy.py"


def test_a_phase_is_reviewed_by_the_adversary_and_the_arbiter_whatever_its_size(cartridge) -> None:
    scripted = ScriptedRunner({"review_phase": APPROVE, "review_adversary": ADVERSARY_REVISES, "arbitrate": ARBITER_REVISES})
    result = phase_review.run(args(cartridge, "review_phase", "review_adversary", "arbitrate"), scripted)
    assert [c["role"] for c in scripted.calls] == ["review_phase", "review_adversary", "arbitrate"]
    assert (result["verdict"], result["reason"], result["review_tier"]) == ("revise", "the copy is dead", 1)


def test_a_reject_quarantines_the_phase_with_the_reviewer_s_reason(cartridge) -> None:
    result = phase_review.run(args(cartridge, "review_phase"), ScriptedRunner({"review_phase": REJECT}))
    assert (result["verdict"], result["reason"]) == ("quarantine", "nothing reaches the notes: the goal is missed")


def test_a_branch_with_no_change_is_quarantined_without_asking_anyone(cartridge) -> None:
    scripted = ScriptedRunner({})
    result = phase_review.run(args(cartridge, "review_phase", patch=""), scripted)
    assert (result["verdict"], result["reason"], scripted.calls) == (
        "quarantine", "the phase branch holds no change against its base", [])


def test_a_reviewer_that_fails_or_abstains_decides_nothing(cartridge) -> None:
    failed = phase_review.run(args(cartridge, "review_phase"), ScriptedRunner({"review_phase": RunnerError("down")}))
    assert failed["verdict"] == "" and "review_phase" in failed["harness_fault"]
    placeholder = {"verdict": "revise", "findings": [], "rationale": ""}
    abstained = phase_review.run(args(cartridge, "review_phase"), ScriptedRunner({"review_phase": placeholder}))
    assert abstained["verdict"] == "" and "placeholder" in abstained["harness_fault"]
