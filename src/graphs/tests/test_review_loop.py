"""L6's pure core: the phase review loop's next step, its episodes, its budget, and the one revision task."""

from __future__ import annotations

from harness import review_loop
from harness.review_loop import BUDGET_SPENT, REOPENED, Progress, Round, Step


def _rounds(*verdicts: str) -> list[Round]:
    return [Round(i, v, f"why {i}") for i, v in enumerate(verdicts, start=1)]


def test_a_phase_with_no_round_smokes_then_reviews_round_one():
    assert review_loop.next([], Progress()) == Step("smoke", 1)
    assert review_loop.next([], Progress.of([], [1])) == Step("review", 1)


def test_the_loop_runs_smoke_review_revise_revision_smoke_review_to_approve():
    """The order inside the lane, one fact at a time, as the lane would learn each."""
    seen = []
    rounds: list[Round] = []
    revisions: set[int] = set()
    smoked: set[int] = set()
    answers = iter(["revise", "approve"])
    while (step := review_loop.next(rounds, Progress.of(revisions, smoked))).action != "finish":
        seen.append((step.action, step.round))
        if step.action == "smoke":
            smoked = {step.round}  # a new tip: only its own smoke counts
        elif step.action == "review":
            rounds.append(Round(step.round, next(answers)))
        else:
            revisions.add(step.round)
            smoked = set()
    assert seen == [("smoke", 1), ("review", 1), ("build_revision", 1), ("smoke", 2), ("review", 2)]
    assert step == Step("finish", 2, "approved")


def test_approve_and_quarantine_finish_with_the_quarantine_s_reason():
    assert review_loop.next(_rounds("approve"), Progress()) == Step("finish", 1, "approved")
    assert review_loop.next(_rounds("revise", "quarantine"), Progress.of([1], [])) == Step(
        "finish", 2, "quarantined", "why 2")


def test_a_revise_builds_its_revision_until_git_holds_it_then_the_next_round():
    assert review_loop.next(_rounds("revise"), Progress()) == Step("build_revision", 1)
    assert review_loop.next(_rounds("revise"), Progress.of([1], [])) == Step("smoke", 2)
    assert review_loop.next(_rounds("revise"), Progress.of([1], [2])) == Step("review", 2)


def test_a_third_revise_is_the_cap_and_finishes_quarantined_with_the_budget_spent():
    """Three rounds an episode: a revise in round three would need a fourth round, so it buys no revision."""
    assert review_loop.next(_rounds("revise", "revise"), Progress.of([1], [])) == Step("build_revision", 2)
    assert review_loop.next(_rounds("revise", "revise", "revise"), Progress.of([1, 2], [])) == Step(
        "finish", 3, "quarantined", BUDGET_SPENT)
    assert review_loop.next(_rounds("revise"), Progress(), cap=1) == Step("finish", 1, "quarantined", BUDGET_SPENT)


def test_the_money_running_out_stops_the_revisions_and_no_budget_means_none_is_counted():
    assert review_loop.next(_rounds("revise"), Progress(), spent=2.0, budget=2.0) == Step(
        "finish", 1, "quarantined", BUDGET_SPENT)
    assert review_loop.next(_rounds("revise"), Progress(), spent=1.99, budget=2.0) == Step("build_revision", 1)
    assert review_loop.next(_rounds("revise"), Progress(), spent=50.0, budget=0.0) == Step("build_revision", 1)


def test_a_reopened_phase_never_finishes_on_the_last_episode_s_approve():
    """(a) of L1's review: the chair's `reopened` row closes the episode; the next round is numbered after it."""
    old = [*_rounds("revise", "approve"), Round(3, REOPENED)]
    assert review_loop.episode(old) == ()
    assert review_loop.next(old, Progress.of([1], [])) == Step("smoke", 4)
    assert review_loop.next([*old, Round(4, "approve")], Progress()) == Step("finish", 4, "approved")


def test_a_reopened_episode_gets_its_own_three_rounds():
    old = [*_rounds("revise", "revise", "quarantine"), Round(4, REOPENED), Round(5, "revise")]
    assert [r.number for r in review_loop.episode(old)] == [5]
    assert review_loop.next(old, Progress.of([1, 2], [])) == Step("build_revision", 5)


def test_recorded_writes_a_revise_the_budget_cannot_buy_as_its_quarantine():
    rounds = _rounds("revise", "revise")
    assert review_loop.recorded(rounds, 3, "revise", "dup", Progress.of([1, 2], [3])) == (
        "quarantine", f"{BUDGET_SPENT}: dup")
    assert review_loop.recorded(rounds[:1], 2, "revise", "dup", Progress.of([1], [2])) == ("revise", "dup")
    assert review_loop.recorded(rounds, 3, "approve", "fine", Progress()) == ("approve", "fine")


def test_phase_verdict_maps_a_reject_to_quarantine():
    assert [review_loop.phase_verdict(v) for v in ("approve", "revise", "reject", "")] == [
        "approve", "revise", "quarantine", "quarantine"]


def test_rounds_of_reads_store_rows_in_round_order():
    rows = [{"round": 2, "verdict": "approve", "reason": "", "revision_json": {}, "cost_usd": 0.5},
            {"round": 1, "verdict": "revise", "reason": "dup", "revision_json": {"id": "p1-rev-1"}, "cost_usd": 1}]
    assert review_loop.rounds_of(rows) == (Round(1, "revise", "dup", {"id": "p1-rev-1"}, 1.0),
                                           Round(2, "approve", "", {}, 0.5))


def test_the_revision_task_carries_what_the_reviewers_asked_and_the_phase_goal():
    output = {
        "phase": "p1", "goal": "Ship  three notes.", "reason": "a helper is duplicated",
        "review": {"verdict": "revise", "rationale": "fat", "findings": [
            {"charter_principle": "no fat", "detail": "lib/copy.py duplicates lib/helper.py", "file": "lib/copy.py"},
            {"charter_principle": "", "detail": "  ", "file": "x"}]},
        "adversary": {"verdict": "revise", "objections": [{"claim": "it is used", "why_wrong": "nothing imports it"}],
                      "strongest_objection": "dead"},
        "arbitration": {"verdict": "revise", "sided_with": "adversary", "reasoning": "dead"},
    }
    task = review_loop.revision_task(1, output)
    assert {k: task[k] for k in ("id", "phase", "kind", "round", "needs", "surfaces", "state")} == {
        "id": "p1-rev-1", "phase": "p1", "kind": "revision", "round": 1, "needs": [], "surfaces": ["lib/copy.py"],
        "state": "ready"}
    body = str(task["body"])
    assert "Review round 1 of phase p1" in body and "The phase's goal: Ship three notes." in body
    assert "- lib/copy.py: lib/copy.py duplicates lib/helper.py [no fat]" in body
    assert "- it is used: nothing imports it" in body
    assert "- x:" not in body  # a finding with nothing in it asks for nothing


def test_the_revision_task_leaves_out_the_adversary_the_arbiter_overruled_and_falls_back_to_the_reason():
    output = {"phase": "p2", "reason": "trim it", "review": {"findings": []},
              "adversary": {"verdict": "revise", "objections": [{"claim": "c", "why_wrong": "w"}]},
              "arbitration": {"verdict": "revise", "sided_with": "charter", "reasoning": "r"}}
    body = str(review_loop.revision_task(2, output)["body"])
    assert "- trim it" in body and "- c: w" not in body
    assert review_loop.revision_task(2, output)["id"] == review_loop.revision_id("p2", 2) == "p2-rev-2"
