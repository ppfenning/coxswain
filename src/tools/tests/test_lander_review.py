"""D2: the new model's pull-request gate. `pr_gate` (pure, no mocks) on example inputs; then the lander's passes over a
real repo, a real bare origin and a real store (`test_lander_models.Models`), the forge the stand-in it is there. The
initiative review's rounds and suite rows are written into the store as the worker's lane writes them."""

from __future__ import annotations

import pytest

from agent_tools import lander, lander_hold, lander_review
from core import landing_record as lr
from harness import initiative_review as ir
from harness import landing_store as ls
from harness import review_loop
from harness.store_steps import put_round, put_step, round_row, step_key, step_row
from tests.test_lander import run, task
from tests.test_lander_models import Models

R = review_loop.Round
NEW: lr.LandingRecord = {"initiative": "n", "lane_model": "phase"}
OLD: lr.LandingRecord = {"initiative": "o"}
TIP, OTHER = "a" * 40, "b" * 40


def _suite(ok: bool, tip: str = TIP, reason: str = "") -> ir.SuiteSeen:
    return ir.SuiteSeen(ok, tip, reason)


# -- the pure gate ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("rounds", "suite", "want"), [
    ([], None, "wait"),
    ([R(1, "revise", "dedupe")], None, "wait"),
    ([R(1, "revise"), R(2, "approve")], None, "wait"),
    ([R(1, "approve")], _suite(True, OTHER), "wait"),  # a suite at another tip says nothing of this one
    ([R(1, "approve")], _suite(True), "open"),
    ([R(1, "approve")], _suite(False, reason="the suite failed: test_x"), "hold"),
    ([R(1, "quarantine", "goal missed")], None, "hold"),
    ([R(1, "quarantine", "goal missed")], _suite(True), "hold"),
    ([R(1, "approve"), R(2, review_loop.REOPENED)], _suite(True), "wait"),  # the last episode's approve counts for nothing
    ([R(1, "revise"), R(2, "approve"), R(3, review_loop.REOPENED), R(4, "approve")], _suite(True), "open"),
])
def test_the_pull_request_opens_only_on_the_episode_s_approve_and_a_passing_suite_at_the_tip(rounds, suite, want):
    assert lander_review.pr_gate(NEW, rounds, suite, TIP) == want


def test_an_old_model_record_is_never_gated():
    assert lander_review.pr_gate(OLD, [], None, TIP) == "open"
    assert lander_review.pr_gate(OLD, [R(1, "quarantine")], _suite(False), TIP) == "open"
    assert lander_review.pr_note(lander_review.decide(OLD, [], None, TIP)) == ""


def test_the_gate_says_why_and_a_hold_names_its_cause():
    held = lander_review.decide(NEW, [R(1, "approve")], _suite(False, reason="the suite failed: tests/test_x.py"), TIP)
    assert (held.gate, held.cause, held.reason) == ("hold", "suite_failed", f"the suite failed at {TIP[:12]}: tests/test_x.py")
    quarantined = lander_review.decide(NEW, [R(2, "quarantine", "goal missed")], None, TIP)
    assert (quarantined.cause, quarantined.reason) == ("quarantined", "the initiative review quarantined it in round 2: goal missed")
    assert "revise-1" in lander_review.decide(NEW, [R(1, "revise")], None, TIP).reason
    opened = lander_review.decide(NEW, [R(1, "revise"), R(2, "approve")], _suite(True), TIP)
    assert lander_review.pr_note(opened) == (f"Initiative review: the initiative review approved it in round 2 and the "
                                             f"suite passed at {TIP[:12]}.")


def test_a_review_hold_keeps_the_merge_hold_s_rules():
    hold = lander_hold.review_hold(None, TIP, "the suite failed at x", "t1", "suite_failed")
    assert lander_hold.is_review_hold(hold) and lander_hold.is_merge_hold(hold)
    assert (hold["phase"], hold["tip"], hold["cause"], hold["attempts"]) == ("", TIP, "suite_failed", 1)
    again = lander_hold.review_hold({**hold, "released": True}, TIP, "the suite failed at x", "t2", "suite_failed")
    assert (again["attempts"], again["since"], again["last_attempt"]) == (2, "t1", "t2")
    assert lander_hold.review_hold(hold, OTHER, "the suite failed at y", "t3", "suite_failed")["attempts"] == 1
    assert not lander_hold.retry_due(hold, "2099-01-01T00:00:00Z")  # never retried on its own
    released, said = lander_hold.release({"initiative": "n", "held": hold})
    assert released is not None and released["held"]["released"] is True and "its pull-request gate" in said
    assert lander_hold.reconciled_hold(hold, None, "t4") == (hold, False)  # reconciliation keeps it


# -- the lander's passes ------------------------------------------------------------------------------------------------

@pytest.fixture
def models(tmp_path) -> Models:
    return Models(tmp_path)


def _assembled(models: Models) -> str:
    """A new-model initiative whose two phases merged cleanly: its land is queued; origin's `init/n` tip."""
    models.start_new("n", [task("a", "p1"), task("b", "p2")])
    models.lane("n", "p1", "one.txt", "one\n")
    models.lane("n", "p2", "two.txt", "two\n")
    outcomes = lander.run_once(models.ports, phase_cap=5)
    assert [o.status for o in outcomes[:2]] == ["done", "done"], outcomes
    return models.on_origin("init/n")


def _round(models: Models, number: int, verdict: str, head: str, reason: str = "") -> None:
    put_round(models.conn, round_row(initiative="n", phase="", round_=number, verdict=verdict, reason=reason,
                                     head_sha=head, run_id="n.initiative-1", worker="w1", claim_epoch=1,
                                     decided_at=models.now()))


def _suite_row(models: Models, number: int, head: str, ok: bool, run_id: str = "n.initiative-1") -> None:
    put_step(models.conn, step_row(run_id=run_id, step=step_key("phase", "", "suite", number), initiative="n", phase="",
                                   task_id="", kind="suite", round_=number, ok=ok, verdict="passed" if ok else "failed",
                                   result={"reason": "" if ok else "the suite failed: tests/test_merge.py"},
                                   finished_at=models.now(), subject_sha=head, worker="w1", claim_epoch=1))


def _land(models: Models) -> lander.Outcome:
    outcomes = [o for o in lander.run_once(models.ports, phase_cap=5) if o.item == ls.item_id(ls.INITIATIVE_LAND, "n")]
    assert len(outcomes) == 1, outcomes
    return outcomes[0]


def test_no_pull_request_opens_before_the_review_approves_and_the_suite_passes_at_the_tip(models: Models) -> None:
    tip = _assembled(models)
    first = _land(models)
    assert first.status == "waiting" and "the initiative review has not decided a round yet" in first.detail, first
    _round(models, 1, "revise", tip, "dedupe")
    assert "asked for revise-1" in _land(models).detail
    _round(models, 2, "approve", tip)
    assert f"the suite has not run at {tip[:12]}" in _land(models).detail
    _suite_row(models, 2, "c" * 40, True)  # a pass at another tip
    assert f"the suite has not run at {tip[:12]}" in _land(models).detail
    assert models.forge.prs == {} and models.notes == []
    _suite_row(models, 2, tip, True, run_id="n.initiative-2")
    opened = _land(models)
    assert any(e.startswith("opened the pull request for n") for e in opened.events), opened
    body = models.forge.prs["init/n"]["body"]
    assert f"Initiative review: the initiative review approved it in round 2 and the suite passed at {tip[:12]}." in body
    assert models.record("n")["state"] in ("pr_open", "landed")


def test_a_suite_that_fails_at_the_tip_holds_the_initiative_and_tells_the_chair_once(models: Models) -> None:
    tip = _assembled(models)
    _round(models, 1, "approve", tip)
    _suite_row(models, 1, tip, False)
    held = _land(models)
    hold = models.record("n")["held"]
    assert (hold["cause"], hold["tip"], hold["attempts"]) == ("suite_failed", tip, 1), hold
    assert hold["reason"] == f"{lander_hold.REVIEW_HOLD}the suite failed at {tip[:12]}: tests/test_merge.py"
    assert held.status == "waiting" and "n is held" in held.detail
    assert [n for n in models.notes if n[0] == "needs_chair"] == [("needs_chair", {"initiative": "n", "reason": f"held: {hold['reason']}"})]
    assert models.forge.prs == {}
    again = _land(models)
    assert again.status == "waiting" and models.record("n")["held"] == hold
    assert len([n for n in models.notes if n[0] == "needs_chair"]) == 1, "a hold that stands is not told again"
    # An operator releases it: the next pass decides again, holds again (counted) and tells the answer.
    released, _ = lander.release_merge_hold(models.ports, "n")
    assert released
    _land(models)
    again_held = models.record("n")["held"]
    assert (again_held["attempts"], again_held.get("released")) == (2, None), again_held
    assert len([n for n in models.notes if n[0] == "needs_chair"]) == 2
    assert models.forge.prs == {}


def test_a_hold_lifts_once_new_work_moves_the_tip_and_the_suite_passes_there(models: Models) -> None:
    tip = _assembled(models)
    _round(models, 1, "approve", tip)
    _suite_row(models, 1, tip, False)
    _land(models)
    assert lander_hold.is_review_hold(models.record("n")["held"])
    # A revision phase merges (new work on the branch): the tip moves, the gate no longer holds there.
    models.rows["n"].append(task("revise-1", "revise-1"))
    models.lane("n", "revise-1", "fix.txt", "fixed\n")
    lander.run_once(models.ports, phase_cap=5)
    moved = models.on_origin("init/n")
    assert moved != tip and not models.record("n").get("held"), models.record("n").get("held")
    _round(models, 2, "approve", moved)
    _suite_row(models, 2, moved, True, run_id="n.initiative-2")
    opened = _land(models)
    assert any(e.startswith("opened the pull request for n") for e in opened.events), opened


def test_a_quarantine_holds_until_a_person_moves_it(models: Models) -> None:
    tip = _assembled(models)
    _round(models, 1, "quarantine", tip, "the goal is missed")
    _land(models)
    hold = models.record("n")["held"]
    assert (hold["cause"], hold["reason"]) == (
        "quarantined", f"{lander_hold.REVIEW_HOLD}the initiative review quarantined it in round 1: the goal is missed")
    assert models.forge.prs == {}


def test_an_old_model_initiative_lands_as_it_always_has(models: Models) -> None:
    models.start("o", [task("a", "p1")])
    models.build("o", "p1")
    run(models.repo, "push", "-q", "origin", "init/o")
    outcomes = lander.run_once(models.ports, phase_cap=5)
    assert any(any(e.startswith("opened the pull request for o") for e in o.events) for o in outcomes), outcomes
    assert "Initiative review" not in models.forge.prs["init/o"]["body"]
