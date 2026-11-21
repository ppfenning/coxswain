"""D2: the new model's pull-request gate. `pr_gate` (pure, no mocks) on example inputs; then the lander's passes over a
real repo, a real bare origin and a real store (`test_lander_models.Models`), the forge the stand-in it is there. The
initiative review's rounds and suite rows are written into the store as the worker's lane writes them."""

from __future__ import annotations

import pytest

from agent_tools import chair_initiative_review, lander, lander_hold, lander_pr, lander_release, lander_review
from core import landing_record as lr
from harness import initiative_review as ir
from harness import landing_ops as ops
from harness import landing_store as ls
from harness import review_loop, store_api, store_phases
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
                                             f"suite passed at {TIP[:12]}. The full suite passed at `{TIP}`.")
    # A quarantine holds only at the tip its round reviewed: once the branch moves the review runs again.
    moved = lander_review.decide(NEW, [R(1, "quarantine", "goal missed")], None, OTHER, reviewed=TIP)
    assert (moved.gate, moved.cause) == ("wait", None) and "the review runs again there" in moved.reason
    assert lander_review.pr_gate(NEW, [R(1, "quarantine")], None, TIP, reviewed=TIP) == "hold"


@pytest.mark.parametrize(("local", "origin", "moves", "want"), [
    (TIP, TIP, 0, ("open", False)),
    (TIP, TIP, 5, ("open", False)),  # at the approved tip it opens, however often it moved before
    (OTHER, TIP, 0, ("wait", True)),  # only the parent merge moved it: push the refreshed branch, and wait
    (OTHER, OTHER, 0, ("wait", False)),  # a phase merge moved origin: nothing to push; the next pass fetches it
    ("c" * 40, OTHER, 1, ("wait", False)),  # origin moved under the refresh: a push would be refused
    (None, None, 0, ("wait", False)),
    (OTHER, TIP, lander_review.MOVE_LIMIT - 1, ("hold", True)),  # the MOVE_LIMIT-th move in a row holds
])
def test_the_pull_request_opens_only_at_the_tip_the_suite_passed_at(local, origin, moves, want):
    step = lander_review.refresh_step(TIP, local, origin, moves)
    assert (step.action, step.push) == want, step
    if step.action == "hold":
        assert "its parent moves faster than the suite runs" in step.reason


def test_a_merge_hold_set_over_a_review_hold_keeps_it_beside_and_gives_it_back():
    review = {**lander_hold.review_hold(None, TIP, "the suite failed at x", "t1", "suite_failed"), "drift": "d"}
    merge = lander_hold.beside_review(lander_hold.merge_hold("p3", OTHER, "conflict", "t2"), review)
    assert merge["phase"] == "p3" and merge["review"]["cause"] == "suite_failed" and "drift" not in merge["review"]
    assert lr.holds_merges(merge), "the merge hold holds merges; the review hold beside it does not lift that"
    again = lander_hold.beside_review(lander_hold.merge_hold("p3", OTHER, "conflict", "t3"), merge)
    assert again["review"] == merge["review"], "a merge hold that replaces one keeps the review hold it kept"
    back = lander_hold.without_merge_hold({**merge, "drift": "a drift"}, "t4")
    assert back is not None and lander_hold.is_review_hold(back) and back["tip"] == TIP and back["drift"] == "a drift"
    assert lander_hold.without_merge_hold(lander_hold.merge_hold("p3", OTHER, "x", "t2"), "t4") is None


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
    released, _ = lander_release.release_merge_hold(models.ports, "n")
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


def _move_main(models: Models, name: str) -> str:
    """Another pull request lands on main, on origin: the parent the land takes forward moves."""
    run(models.worker, "fetch", "-q", "origin")
    run(models.worker, "checkout", "-q", "-B", "main", "origin/main")
    (models.worker / name).write_text(f"{name}\n")
    run(models.worker, "add", "-A")
    run(models.worker, "commit", "-qm", f"main: {name}")
    run(models.worker, "push", "-q", "origin", "HEAD:refs/heads/main")
    return run(models.worker, "rev-parse", "HEAD")


def _approve(models: Models, number: int, tip: str) -> None:
    _round(models, number, "approve", tip)
    _suite_row(models, number, tip, True, run_id=f"n.initiative-{number}")


def test_main_moving_after_a_passing_suite_opens_no_pull_request_at_the_old_tip(models: Models) -> None:
    tip = _assembled(models)
    _approve(models, 1, tip)
    _move_main(models, "elsewhere.txt")
    first = _land(models)
    assert first.status == "waiting" and "after the suite passed" in first.detail, first
    assert models.forge.prs == {}, "no pull request at a tip the suite never ran at"
    moved = models.on_origin("init/n")
    assert moved != tip and models.record("n")["review_tip"] == {"tip": moved, "moves": 1}, "pushed, and recorded"
    again = _land(models)
    assert again.status == "waiting" and f"the suite has not run at {moved[:12]}" in again.detail
    assert models.forge.prs == {}
    _approve(models, 2, moved)
    opened = _land(models)
    assert any(e.startswith("opened the pull request for n") for e in opened.events), opened
    assert models.on_origin("init/n") == moved
    assert f"The full suite passed at `{moved}`." in models.forge.prs["init/n"]["body"]


def test_a_parent_that_outpaces_the_suite_holds_the_pull_request_and_tells_the_chair_once(models: Models) -> None:
    tip = _assembled(models)
    for number in range(1, lander_review.MOVE_LIMIT + 1):
        _approve(models, number, tip)
        _move_main(models, f"m{number}.txt")
        _land(models)
        tip = models.on_origin("init/n")
    hold = models.record("n")["held"]
    assert (hold["cause"], hold["tip"]) == ("outpaced", tip) and "faster than the suite runs" in hold["reason"], hold
    told = [n for n in models.notes if n[0] == "needs_chair"]
    assert len(told) == 1 and "faster than the suite runs" in str(told[0][1]["reason"])
    _approve(models, 9, tip)  # even a pass at the new tip waits on the operator: nothing lifts it on its own
    assert _land(models).status == "waiting" and models.record("n")["held"]["cause"] == "outpaced"
    assert models.forge.prs == {} and len([n for n in models.notes if n[0] == "needs_chair"]) == 1
    released, _ = lander_release.release_merge_hold(models.ports, "n")
    assert released
    opened = _land(models)
    assert any(e.startswith("opened the pull request for n") for e in opened.events), opened
    assert models.record("n")["review_tip"]["moves"] == 0


def test_a_phase_merge_that_lands_between_the_gate_and_the_push_opens_no_pull_request(models: Models, monkeypatch) -> None:
    tip = _assembled(models)
    _approve(models, 1, tip)
    refresh = ops.take_parent_forward

    def racing(*args, **kwargs):  # the gate read origin at `tip`; a phase merge lands on origin while the land refreshes
        out = refresh(*args, **kwargs)
        run(models.worker, "fetch", "-q", "origin")
        run(models.worker, "checkout", "-q", "-B", "init/n", "origin/init/n")
        (models.worker / "late.txt").write_text("late\n")
        run(models.worker, "add", "-A")
        run(models.worker, "commit", "-qm", "a phase merge lands")
        run(models.worker, "push", "-q", "origin", "HEAD:refs/heads/init/n")
        monkeypatch.setattr(ops, "take_parent_forward", refresh)
        return out

    monkeypatch.setattr(ops, "take_parent_forward", racing)
    _move_main(models, "elsewhere.txt")
    raced = _land(models)
    late = models.on_origin("init/n")
    assert raced.status == "waiting" and "after the suite passed" in raced.detail and models.forge.prs == {}, raced
    assert late != tip and models.record("n")["review_tip"]["tip"] == late, "origin's tip is what the chair reviews"
    assert run(models.repo, "rev-parse", "init/n") == late, "this clone gave up its refresh for origin's tip"
    _approve(models, 2, late)
    again = _land(models)
    pushed = models.on_origin("init/n")
    assert again.status == "waiting" and pushed != late and models.forge.prs == {}, "main is not in origin's tip yet"
    _approve(models, 3, pushed)
    opened = _land(models)
    assert any(e.startswith("opened the pull request for n") for e in opened.events), opened
    assert f"`{pushed}`" in models.forge.prs["init/n"]["body"]


def test_a_quarantine_at_an_old_tip_lifts_once_the_fix_moves_the_branch(models: Models) -> None:
    tip = _assembled(models)
    _round(models, 1, "quarantine", tip, "the goal is missed")
    _land(models)
    assert models.record("n")["held"]["cause"] == "quarantined"
    models.rows["n"].append(task("fix-1", "fix-1"))
    models.lane("n", "fix-1", "fix.txt", "fixed\n")
    lander.run_once(models.ports, phase_cap=5)
    moved = models.on_origin("init/n")
    assert moved != tip and not models.record("n").get("held")
    assert models.record("n")["review_tip"]["tip"] == moved, "the chair reads the moved tip and reviews again"


def test_a_failed_suite_a_fix_a_reopen_and_a_pass_open_the_pull_request_at_the_new_tip(models: Models) -> None:
    """The whole path through the store: the chair's plan (`review_changes`) read from what the lander wrote, its
    changes applied as the tick applies them (`store_api.phase_changes_on`)."""
    def plan() -> list[dict]:
        records = [models.record("n")]
        rows, rounds = store_phases.list_rows(models.conn), {"n": store_phases.review_rounds(models.conn, "n", "")}
        tasks = [{"initiative": "n", "task_id": t["id"], "phase": t["phase"], "state": t["state"]} for t in models.rows["n"]]
        return chair_initiative_review.review_changes(records, tasks, rows, rounds, lambda repo: [])

    tip = _assembled(models)
    (insert,) = plan()
    store_api.phase_changes_on(models.conn, [insert], "chair")
    _round(models, 1, "approve", tip)
    _suite_row(models, 1, tip, False)
    models.conn.execute("UPDATE phase_queue SET state = 'finished', outcome = 'failed' WHERE initiative = 'n'")
    _land(models)
    assert models.record("n")["held"]["cause"] == "suite_failed" and plan() == []  # held: nothing to queue
    models.rows["n"].append(task("fix", "fix"))
    models.lane("n", "fix", "fix.txt", "fixed\n")
    lander.run_once(models.ports, phase_cap=5)
    fixed = models.on_origin("init/n")
    (reopen,) = plan()
    assert reopen["op"] == "reopen" and fixed[:12] in reopen["reason"], reopen
    store_api.phase_changes_on(models.conn, [reopen], "chair")
    row = store_phases.get_row(models.conn, "n", ir.QUEUE_PHASE)
    assert row is not None and row["state"] == "ready"
    assert review_loop.episode(review_loop.rounds_of(store_phases.review_rounds(models.conn, "n", ""))) == ()
    assert plan() == [], "reopened once"
    _round(models, 3, "approve", fixed)
    _suite_row(models, 3, fixed, True, run_id="n.initiative-3")
    opened = _land(models)
    assert any(e.startswith("opened the pull request for n") for e in opened.events), opened
    assert f"The full suite passed at `{fixed}`." in models.forge.prs["init/n"]["body"]


def test_a_phase_merge_hold_keeps_a_review_hold_beside_it(models: Models) -> None:
    tip = _assembled(models)
    _approve(models, 1, tip)
    review = lander_hold.review_hold(None, tip, "the suite failed at x", models.now(), "suite_failed")
    ls.put_record(models.conn, {**models.record("n"), "held": review})
    done = ops.PhaseMerge("conflicted", "n", "p3", detail="refused", cause="conflict")
    lander._hold_phase(models.ports, models.record("n"), "p3", done, "d" * 40)
    hold = models.record("n")["held"]
    assert hold["phase"] == "p3" and hold["review"]["cause"] == "suite_failed" and "drift" not in hold, hold
    assert lander_hold.without_merge_hold(hold, models.now())["cause"] == "suite_failed"


def test_an_old_model_initiative_lands_as_it_always_has(models: Models) -> None:
    models.start("o", [task("a", "p1")])
    models.build("o", "p1")
    run(models.repo, "push", "-q", "origin", "init/o")
    outcomes = lander.run_once(models.ports, phase_cap=5)
    assert any(any(e.startswith("opened the pull request for o") for e in o.events) for o in outcomes), outcomes
    assert "Initiative review" not in models.forge.prs["init/o"]["body"]
    assert all("head_sha" not in step for step in getattr(models.forge, "steps", [])), "the old model's merge is not pinned"


# -- wait for fresh: an open pull request lands only at a head the suite passed at -----------------------------------

OPEN = lander_review.Decision("open", "the initiative review approved it in round 1 and the suite passed at aaaa", None, TIP)
WAIT = lander_review.Decision("wait", "the suite has not run there", None, OTHER)


@pytest.mark.parametrize(("decision", "head", "pr", "want"), [
    (OPEN, TIP, {"passed": TIP, "at": TIP}, (False, TIP, "", False)),  # fresh, and the body already says so
    (OPEN, OTHER, {"passed": TIP, "at": TIP}, (True, TIP, "draft", True)),  # the land just moved the head past it
    (WAIT, OTHER, {"passed": TIP, "at": TIP}, (True, TIP, "draft", True)),
    (WAIT, OTHER, {"passed": TIP, "at": OTHER, "stale": True}, (True, TIP, "", False)),  # already a draft, said
    (WAIT, "c" * 40, {"passed": TIP, "at": OTHER, "stale": True}, (True, TIP, "", True)),  # moved again: said again
    (lander_review.Decision("open", "ok", None, OTHER), OTHER, {"passed": TIP, "at": OTHER, "stale": True},
     (False, OTHER, "ready", True)),  # the suite passed at the head: ready, and it names the new sha
    (lander_review.Decision("hold", "the suite failed", "suite_failed", TIP), TIP, {"passed": "", "at": ""},
     (True, "", "draft", True)),
])
def test_an_open_pull_request_is_a_draft_while_its_head_has_no_passing_suite(decision, head, pr, want):
    fresh = lander_review.freshness(decision, head, pr)
    assert (fresh.stale, fresh.passed, fresh.mark, fresh.report) == want, fresh


def test_the_body_names_the_sha_that_last_passed_and_says_when_the_head_is_ahead_of_it():
    stale = lander_review.freshness(WAIT, OTHER, {"passed": TIP})
    assert f"last passed at `{TIP}`" in stale.note and f"the head is at `{OTHER}`, ahead of it" in stale.note
    assert "cannot land until the suite passes" in stale.note
    assert "has not passed" in lander_review.freshness(WAIT, OTHER, {}).note
    assert lander_review.freshness(OPEN, TIP, {}).note == lander_review.pr_note(OPEN)


def test_the_lander_s_own_merge_reads_the_head_again_and_the_count_starts_again_once_the_pull_request_opens():
    assert lander_review.head_moved(TIP, TIP) == ""
    assert OTHER[:12] in lander_review.head_moved(TIP, OTHER) and "unread" in lander_review.head_moved(TIP, None)
    pr, tip = lander_review.opened({"initiative": "n", "review_tip": {"tip": OTHER, "moves": 2}}, TIP)
    assert (pr, tip) == ({"stale": False, "passed": TIP, "at": TIP}, {"tip": TIP, "moves": 0})


def _plan(models: Models) -> list[dict]:
    """The chair's initiative review plan, read from what the lander wrote (`review_changes`)."""
    rows, rounds = store_phases.list_rows(models.conn), {"n": store_phases.review_rounds(models.conn, "n", "")}
    tasks = [{"initiative": "n", "task_id": t["id"], "phase": t["phase"], "state": t["state"]} for t in models.rows["n"]]
    return chair_initiative_review.review_changes([models.record("n")], tasks, rows, rounds, lambda repo: [])


def _opened(models: Models) -> str:
    """The pull request open at a passing tip, its CI still running so it stays open; the review row finished
    `approved` as the worker finishes it. That tip."""
    tip = _assembled(models)
    (insert,) = _plan(models)
    store_api.phase_changes_on(models.conn, [insert], "chair")
    _approve(models, 1, tip)
    models.conn.execute("UPDATE phase_queue SET state = 'finished', outcome = 'approved' WHERE initiative = 'n'")
    models.forge.checks = ("pending", "CI is running")
    opened = _land(models)
    assert opened.status == "waiting" and any(e.startswith("opened the pull request for n") for e in opened.events), opened
    pr = models.record("n")["pr"]
    assert (pr["passed"], pr["at"], pr["stale"], models.record("n")["review_tip"]["moves"]) == (tip, tip, False, 0), pr
    return tip


def _merges(models: Models) -> list:
    return [c for c in models.forge.calls if c[0] == "merge"]


def test_main_moving_under_an_open_pull_request_makes_it_a_draft_until_a_fresh_suite_passes_at_the_new_head(models) -> None:
    tip = _opened(models)
    models.forge.checks = ("green", "all checks passed")
    _move_main(models, "elsewhere.txt")
    stale = _land(models)
    head = models.on_origin("init/n")
    assert stale.status == "waiting" and "after the suite passed" in stale.detail and head != tip, stale
    pr, body = models.record("n")["pr"], models.forge.prs["init/n"]["body"]
    assert (pr["stale"], pr["passed"], pr["at"], models.forge.prs["init/n"]["draft"]) == (True, tip, head, True), pr
    assert f"The full suite last passed at `{tip}`; the head is at `{head}`, ahead of it." in body, body
    assert models.record("n")["review_tip"] == {"tip": head, "moves": 1} and _merges(models) == []
    # Green CI is not the gate: the pull request waits for the suite at its head, and stays a draft.
    again = _land(models)
    assert again.status == "waiting" and f"the suite has not run at {head[:12]}" in again.detail, again
    assert _merges(models) == [] and models.forge.prs["init/n"]["draft"] is True
    # The chair reopens the review with the pull request open; the suite passes at the new head; it is ready and lands.
    (reopen,) = _plan(models)
    assert reopen["op"] == "reopen" and head[:12] in reopen["reason"], reopen
    store_api.phase_changes_on(models.conn, [reopen], "chair")
    _approve(models, 3, head)
    landed = _land(models)
    assert landed.status == "done" and f"n: the pull request is ready: the suite passed at its head {head[:12]}" in landed.events
    assert models.forge.prs["init/n"]["draft"] is False and _merges(models) == [("merge", "init/n")]
    assert f"The full suite passed at `{head}`." in models.forge.prs["init/n"]["body"]
    assert [c[0] for c in models.forge.calls if c[0] in ("draft", "ready", "merge")] == ["draft", "ready", "merge"]
    assert models.forge.steps[-1]["head_sha"] == head, "the merge is pinned to the head the suite passed at"


def test_a_phase_merged_into_an_open_pull_request_makes_it_a_draft_and_a_person_s_gate_waits_for_fresh_too(models) -> None:
    models.gate = "ticket"  # a person merges: the lander never refreshes it, and the draft is what stops them
    tip = _opened(models)
    models.rows["n"].append(task("fix-1", "fix-1"))
    models.lane("n", "fix-1", "fix.txt", "fixed\n")
    lander.run_once(models.ports, phase_cap=5)  # the fix merges, and the land in the same pass sees the head moved
    head = models.on_origin("init/n")
    assert head != tip and models.forge.prs["init/n"]["draft"] is True and models.record("n")["pr"]["stale"] is True
    assert models.record("n")["review_tip"]["tip"] == head
    (reopen,) = _plan(models)
    store_api.phase_changes_on(models.conn, [reopen], "chair")
    _approve(models, 3, head)
    waiting = _land(models)
    assert waiting.status == "waiting" and lander.PERSON_GATE_NOTE in waiting.detail, waiting
    assert models.forge.prs["init/n"]["draft"] is False and _merges(models) == []


def test_a_parent_that_outpaces_the_suite_under_an_open_pull_request_holds_it_a_draft(models) -> None:
    head = _opened(models)
    models.forge.checks = ("green", "all checks passed")
    for number in range(2, lander_review.MOVE_LIMIT + 2):
        _move_main(models, f"m{number}.txt")
        _land(models)
        head = models.on_origin("init/n")
        _approve(models, number, head)
    hold = models.record("n")["held"]
    assert (hold["cause"], hold["tip"]) == ("outpaced", head) and "faster than the suite runs" in hold["reason"], hold
    # A pass at the head does not lift it: only an operator's release does, and the pull request stays a draft.
    assert _land(models).status == "waiting" and _merges(models) == [] and models.forge.prs["init/n"]["draft"] is True
    released, _ = lander_release.release_merge_hold(models.ports, "n")
    assert released and _land(models).status == "done" and _merges(models) == [("merge", "init/n")]


def test_a_head_that_moves_between_the_gate_and_the_merge_is_not_merged(models: Models, monkeypatch) -> None:
    tip = _opened(models)
    poll = models.forge.poll_checks

    def racing(repo, *, ref, memo=None):  # a phase merge lands on origin while the land reads the checks
        run(models.worker, "fetch", "-q", "origin")
        run(models.worker, "checkout", "-q", "-B", "init/n", "origin/init/n")
        (models.worker / "late.txt").write_text("late\n")
        run(models.worker, "add", "-A")
        run(models.worker, "commit", "-qm", "a phase merge lands")
        run(models.worker, "push", "-q", "origin", "HEAD:refs/heads/init/n")
        monkeypatch.setattr(models.forge, "poll_checks", poll)
        return ("green", "all checks passed", dict(memo or {}))

    monkeypatch.setattr(models.forge, "poll_checks", racing)
    raced = _land(models)
    assert raced.status == "waiting" and f"the head moved from {tip[:12]}" in raced.detail and _merges(models) == [], raced
    late = _land(models)
    assert late.status == "waiting" and models.forge.prs["init/n"]["draft"] is True and _merges(models) == []


# -- the push that moves an open pull request's head: a draft first --------------------------------------------------

def _fix_phase(models: Models) -> lander.Outcome:
    """A fix phase built and merged into the open pull request's branch; the phase merge's outcome."""
    models.rows["n"].append(task("fix-1", "fix-1"))
    models.lane("n", "fix-1", "fix.txt", "fixed\n")
    outcomes = lander.run_once(models.ports, phase_cap=5)
    (merged,) = [o for o in outcomes if o.item == ls.item_id(ls.PHASE_MERGE, "n", "fix-1")]
    return merged


def test_a_phase_merged_into_an_open_pull_request_is_a_draft_before_its_push_is_visible(models: Models, monkeypatch) -> None:
    tip = _opened(models)
    seen: list[tuple] = []
    draft, push = models.forge.mark_draft, models.forge.push

    def marking(repo, branch):
        seen.append(("draft", models.on_origin("init/n")))
        return draft(repo, branch)

    def pushing(repo, branch):  # what the forge and the record say as the push goes out
        seen.append(("push", models.forge.prs["init/n"].get("draft"), models.record("n")["pr"].get("stale")))
        return push(repo, branch)

    monkeypatch.setattr(models.forge, "mark_draft", marking)
    monkeypatch.setattr(models.forge, "push", pushing)
    assert _fix_phase(models).status == "done"
    head = models.on_origin("init/n")
    assert head != tip and seen[:2] == [("draft", tip), ("push", True, True)], seen
    assert models.forge.prs["init/n"]["draft"] is True and models.record("n")["pr"]["stale"] is True


def test_a_draft_the_forge_refuses_means_no_push_and_no_phase_recorded_merged(models: Models, monkeypatch) -> None:
    tip = _opened(models)
    monkeypatch.setattr(models.forge, "mark_draft", lambda repo, branch: (False, "the forge is down"))
    pushes = len([c for c in models.forge.calls if c[0] == "push"])
    merged = _fix_phase(models)
    assert merged.status == "failed" and "could not be made a draft first (the forge is down)" in merged.detail, merged
    assert models.on_origin("init/n") == tip, "nothing reached origin"
    assert len([c for c in models.forge.calls if c[0] == "push"]) == pushes
    record = models.record("n")
    assert lr.phase_state(record, "fix-1", models.rows["n"]) != "merged" and record["pr"]["stale"] is False
    assert run(models.repo, "rev-parse", "init/n") == tip, "the merge made here was unwound"


def test_a_refresh_of_an_open_pull_request_the_forge_will_not_draft_is_not_pushed_and_told_once(models: Models, monkeypatch) -> None:
    tip = _opened(models)
    monkeypatch.setattr(models.forge, "mark_draft", lambda repo, branch: (False, "the forge is down"))
    models.forge.checks = ("green", "all checks passed")
    _move_main(models, "elsewhere.txt")
    for _ in range(lander_review.MARK_TELL_AFTER + 2):
        waiting = _land(models)
        assert waiting.status == "waiting" and "could not be made a draft first" in waiting.detail, waiting
    assert models.on_origin("init/n") == tip and models.record("n")["pr"]["mark_refused"] == lander_review.MARK_TELL_AFTER + 2
    assert run(models.repo, "rev-parse", "init/n") == tip and _merges(models) == [], "nothing untested is kept or merged"
    told = [n for n in models.notes if n[0] == "needs_chair" and "refused to mark the pull request draft" in n[1]["reason"]]
    assert len(told) == 1 and f"{lander_review.MARK_TELL_AFTER} times in a row: the forge is down" in told[0][1]["reason"]
    monkeypatch.undo()  # the forge takes the draft again: the refresh is pushed, and the count starts again
    _land(models)
    pr = models.record("n")["pr"]
    assert models.on_origin("init/n") != tip and pr["stale"] is True and "mark_refused" not in pr, pr


def test_a_refused_ready_mark_is_told_once_and_the_count_starts_again_once_it_is_taken():
    told, record = [], {"initiative": "n", "branch": "init/n", "pr": {"url": "u", "stale": True, "passed": TIP, "at": OTHER}}

    class Store:
        def __init__(self):
            self.record = record

    store = Store()

    class Refusing:
        ok = False

        def mark_draft(self, repo, branch):
            return self.ok, "refused"

        def mark_ready(self, repo, branch):
            return self.ok, "refused"

        def update_pr(self, repo, title, body, *, head):
            return True, ""

    forge = Refusing()
    fresh = lander_review.freshness(lander_review.Decision("open", "ok", None, OTHER), OTHER, record["pr"])
    assert fresh.mark == "ready"
    patched = pytest.MonkeyPatch()
    patched.setattr(ls, "get_record", lambda conn, initiative: store.record)
    patched.setattr(ls, "put_record", lambda conn, rec: setattr(store, "record", rec))
    try:
        for _ in range(lander_review.MARK_TELL_AFTER + 1):
            lander_review.mark_pr(None, forge, None, store.record, fresh, ("T", "B"), "t", lambda k, f: told.append(f))
        assert store.record["pr"]["mark_refused"] == lander_review.MARK_TELL_AFTER + 1 and store.record["pr"]["stale"]
        assert len(told) == 1 and "refused to mark the pull request ready 3 times in a row: refused" in told[0]["reason"]
        forge.ok = True
        lander_review.mark_pr(None, forge, None, store.record, fresh, ("T", "B"), "t", lambda k, f: told.append(f))
        assert "mark_refused" not in store.record["pr"] and store.record["pr"]["stale"] is False
    finally:
        patched.undo()
    assert [lander_review.mark_told(n) for n in range(1, 6)] == [False, False, True, False, False]


# -- an open pull request the land finds again ----------------------------------------------------------------------

def test_a_pull_request_found_open_keeps_its_draft_and_its_moves_and_is_marked_ready_once_fresh(models: Models) -> None:
    tip = _opened(models)
    record = {**models.record("n"), "review_tip": {"tip": tip, "moves": 2}}
    record = {**record, "pr": {**record["pr"], "stale": True, "at": "c" * 40}}
    ls.put_record(models.conn, record)
    models.forge.prs["init/n"]["draft"] = True
    found = lander_pr.push_and_open(models.ports, record, models.rows["n"], models.meta("n"), models.repo,
                                    ("init/n", "main"), "", tip)
    assert not isinstance(found, lander.Outcome), found
    after, events = found
    assert any(e.startswith("found the open pull request") for e in events)
    assert (after["pr"]["stale"], after["review_tip"]["moves"]) == (True, 2), "found, not opened: nothing starts afresh"
    gate = lander_review.Decision("open", "the suite passed", None, tip)
    marked = lander_pr.mark_open_pr(models.ports, "n", gate, tip)
    assert any("the pull request is ready" in e for e in marked) and models.forge.prs["init/n"]["draft"] is False


def test_a_pull_request_opened_here_starts_fresh_at_the_tip_the_suite_passed_at(models: Models) -> None:
    tip = _assembled(models)
    record = {**models.record("n"), "review_tip": {"tip": tip, "moves": 2}}
    opened = lander_pr.push_and_open(models.ports, record, models.rows["n"], models.meta("n"), models.repo,
                                     ("init/n", "main"), "", tip)
    assert not isinstance(opened, lander.Outcome), opened
    after, events = opened
    assert any(e.startswith("opened the pull request for n") for e in events)
    assert (after["pr"]["stale"], after["pr"]["passed"], after["review_tip"]["moves"]) == (False, tip, 0)
