"""D2's initiative review: its pure rules (`harness.initiative_review`) on example inputs, no mocks; its store writes
against a real store (sqlite, and Postgres when COXSWAIN_TEST_PG_URL names one); the `.agent-checks` marker that keeps
the root suite out of the old model's list; and the `phase-review` graph's ticket for a whole initiative."""

from __future__ import annotations

import pytest

from graphs.delivery import phase_review
from harness import initiative_review as ir
from harness import review_loop, store_api, store_phases
from harness.check_gate import tree_gate_checks
from harness.checks import NEW_MODEL_ONLY, for_level, old_model_checks, repo_checks
from harness.store_migrate import open_store
from harness.store_queue import read
from harness.store_steps import round_row, step_key, step_row

AT = "2026-11-21T00:00:00Z"
R = review_loop.Round


# -- the pure rules ---------------------------------------------------------------------------------------------------

def test_the_review_row_s_rounds_are_the_initiative_s_own_and_its_revisions_are_phases_of_their_own():
    assert ir.rounds_phase(ir.QUEUE_PHASE) == "" and ir.rounds_phase("p1") == "p1"
    assert ir.revision_phase(2) == "revise-2"


def test_the_first_step_is_round_one_s_review_with_no_smoke_before_it():
    step, progress = ir.next_step([], frozenset())
    assert (step.action, step.round) == ("review", 1) and progress.smoked == frozenset({1})


def test_a_revise_waits_for_its_revision_phase_to_merge_then_the_next_round_reviews():
    rounds = [R(1, "revise", "dedupe")]
    assert ir.next_step(rounds, {"p1", "p2"})[0].action == "build_revision"
    step, progress = ir.next_step(rounds, {"p1", "p2", "revise-1"})
    assert (step.action, step.round, progress.revisions) == ("review", 2, frozenset({1}))


def test_an_approve_or_a_quarantine_finishes_and_only_the_current_episode_counts():
    assert ir.next_step([R(1, "revise"), R(2, "approve")], {"revise-1"})[0] == review_loop.Step("finish", 2, "approved")
    held = ir.next_step([R(1, "quarantine", "goal missed")], set())[0]
    assert (held.outcome, held.reason) == ("quarantined", "goal missed")
    # The chair reopened the review (new work after the approval): round 3 is the new episode's first.
    reopened = [R(1, "approve"), R(2, review_loop.REOPENED)]
    assert (ir.next_step(reopened, set())[0].action, ir.next_step(reopened, set())[0].round) == ("review", 3)


def test_the_third_revise_of_an_episode_is_the_budget_spent_and_never_a_fourth_revision_phase():
    rounds = [R(1, "revise"), R(2, "revise"), R(3, "revise")]
    step = ir.next_step(rounds, {"revise-1", "revise-2"})[0]
    assert (step.action, step.outcome, step.reason) == ("finish", "quarantined", review_loop.BUDGET_SPENT)
    assert review_loop.unwritten(rounds, step) == R(4, "quarantine", review_loop.BUDGET_SPENT)


def test_the_suite_that_counts_is_the_last_at_the_tip():
    rows = [{"subject_sha": "a", "ok": 0, "run_id": "r1", "result_json": {"reason": "the suite failed: x"}},
            {"subject_sha": "b", "ok": 1, "run_id": "r2", "result_json": {}},
            {"subject_sha": "a", "ok": 1, "run_id": "r3", "result_json": {}}]
    assert ir.suite_at(rows, "a") == ir.SuiteSeen(True, "a", "", "r3")
    assert ir.suite_at(rows[:1], "a") == ir.SuiteSeen(False, "a", "the suite failed: x", "r1")
    assert ir.suite_at(rows, "c") is None and ir.suite_at(rows, "") is None


def test_what_the_worker_reads_is_the_episode_s_last_round_and_the_suite_at_its_tip():
    rounds = [{"round": 1, "verdict": "revise", "reason": "r", "head_sha": "a"},
              {"round": 2, "verdict": "approve", "reason": "", "head_sha": "b"}]
    suites = [{"subject_sha": "a", "ok": 1, "run_id": "x", "result_json": {}},
              {"subject_sha": "b", "ok": 0, "run_id": "y", "result_json": {"reason": "the suite failed: t"}}]
    assert ir.ended(rounds, suites) == ((2, "approve"), "", ir.SuiteSeen(False, "b", "the suite failed: t", "y"))
    assert ir.ended([], suites) == (None, "", None)
    closed = [*rounds, {"round": 3, "verdict": review_loop.REOPENED, "reason": "", "head_sha": ""}]
    assert ir.ended(closed, suites) == (None, "", None)


@pytest.mark.parametrize(("last", "suite", "attempts", "kind", "outcome"), [
    ((2, "approve"), ir.SuiteSeen(True, "b" * 40), 1, "ack", "approved"),
    ((2, "approve"), ir.SuiteSeen(False, "b" * 40, "the suite failed: tests/test_x.py"), 1, "quarantine", "failed"),
    ((1, "quarantine"), None, 1, "quarantine", "quarantined"),
    ((1, "revise"), None, 1, "ack", "revised"),
    ((1, "approve"), None, 1, "retry", ""),
    (None, None, 1, "retry", ""),
    (None, None, 3, "quarantine", "failed"),
])
def test_the_worker_s_verdict_on_an_ended_review(last, suite, attempts, kind, outcome):
    verdict = ir.lane_verdict(last, suite, attempts, reason="why")
    assert (verdict.kind, verdict.outcome) == (kind, outcome)
    if outcome == "failed" and last is not None:
        assert verdict.reason == f"the suite failed at {'b' * 12}: tests/test_x.py"
    if kind == "retry":
        assert verdict.wait_s > 0


def test_a_revise_round_s_revision_is_the_one_task_of_its_own_phase():
    output = {"phase": ir.QUEUE_PHASE, "goal": "ship it", "reason": "two helpers",
              "review": {"findings": [{"file": "lib/copy.py", "detail": "delete lib/copy.py"}]}}
    task = ir.revision_task("ship", 1, output)
    assert (task["id"], task["phase"], task["kind"], task["round"]) == ("revise-1", "revise-1", "revision", 1)
    body = str(task["body"])
    assert body.startswith("Initiative review round 1 of ship asked for one revision of the initiative branch.")
    assert "- lib/copy.py: delete lib/copy.py" in body and "ship it" in body
    assert task["surfaces"] == ["lib/copy.py"]


def test_the_initiative_s_goal_is_its_own_prose():
    assert ir.initiative_goal({"id": "ship", "title": "Ship", "body": "Three notes."}) == "initiative 'ship': Ship\n\nThree notes."
    assert ir.initiative_goal({"id": "ship"}) == "initiative 'ship'"


# -- the store --------------------------------------------------------------------------------------------------------

@pytest.fixture
def conn(store_url):
    c = open_store(store_url, AT)
    yield c
    c.close()


def _review_row(**extra):
    return {"op": "insert", "initiative": "ship", "phase": ir.QUEUE_PHASE, "kind": ir.KIND, "repo": "~/src/app",
            "base_ref": "init/ship", "requires": ["repo:app"], "band": 1, "seq": 2, **extra}


def _suite(run: str, sha: str, ok: bool, round_: int = 1, epoch: int = 1) -> dict:
    return step_row(run_id=run, step=step_key("phase", "", "suite", round_), initiative="ship", phase="", task_id="",
                    kind="suite", round_=round_, ok=ok, verdict="passed" if ok else "failed",
                    result={"reason": "" if ok else "the suite failed: t"}, finished_at="", subject_sha=sha,
                    worker="w1", claim_epoch=epoch)


def test_the_review_s_rows_are_written_under_the_initiative_while_its_claim_is_held(conn):
    store_phases.apply_changes(conn, [_review_row()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", ["repo:app"], 1)
    assert claim is not None and (claim.phase, claim.row["kind"], claim.row["base_ref"]) == (ir.QUEUE_PHASE, ir.KIND, "init/ship")
    decided = round_row(initiative="ship", phase="", round_=1, verdict="approve", head_sha="b", run_id="r1", worker="w1",
                        claim_epoch=claim.epoch)
    # Under the rows' own phase there is no claim: refused, nothing written.
    assert not store_phases.put_steps_fenced(conn, [_suite("r1", "b", True)], "w1", claim.epoch, [decided])
    assert store_phases.review_rounds(conn, "ship", "") == [] and store_phases.suite_rows(conn, "ship") == []
    assert store_phases.put_steps_fenced(conn, [_suite("r1", "b", True)], "w1", claim.epoch, [decided],
                                         claimed=ir.QUEUE_PHASE)
    assert [(r["round"], r["verdict"], r["head_sha"]) for r in store_phases.review_rounds(conn, "ship", "")] == [(1, "approve", "b")]
    assert ir.suite_at(store_phases.suite_rows(conn, "ship"), "b") == ir.SuiteSeen(True, "b", "", "r1")
    assert store_phases.smoke_rows(conn, "ship", "") == []  # the suite is not a smoke
    assert not store_phases.put_steps_fenced(conn, [_suite("r2", "c", True)], "w2", claim.epoch, claimed=ir.QUEUE_PHASE)


def test_a_revised_review_is_claimable_again_in_the_same_episode_and_a_reopen_closes_the_initiative_s(conn):
    store_phases.apply_changes(conn, [_review_row()], "chair-loop")
    claim = store_phases.claim_next(conn, "w1", ["repo:app"], 1)
    revise = round_row(initiative="ship", phase="", round_=1, verdict="revise", head_sha="a", worker="w1",
                       claim_epoch=claim.epoch)
    store_phases.put_steps_fenced(conn, [], "w1", claim.epoch, [revise], claimed=ir.QUEUE_PHASE)
    assert store_phases.finish(conn, claim, "w1", "revised", "round 1 asked for revise-1")
    again = {"op": ir.REREVIEW_OP, "initiative": "ship", "phase": ir.QUEUE_PHASE, "epoch": claim.epoch, "reason": "merged"}
    assert store_phases.apply_changes(conn, [again], "chair-loop") == 1
    row = store_phases.get_row(conn, "ship", ir.QUEUE_PHASE)
    assert (row["state"], row["outcome"], row["attempts"]) == ("ready", "", 0)
    assert [r["verdict"] for r in store_phases.review_rounds(conn, "ship", "")] == ["revise"]  # no episode closed
    assert store_phases.apply_changes(conn, [again], "chair-loop") == 0  # a repeat, or a row that is not `revised`
    # A reopen of the finished review ends the initiative's own episode, under phase ''.
    claim = store_phases.claim_next(conn, "w1", ["repo:app"], 1)
    store_phases.finish(conn, claim, "w1", "approved", "ok")
    reopen = {"op": "reopen", "initiative": "ship", "phase": ir.QUEUE_PHASE, "epoch": claim.epoch, "reason": "new work"}
    assert store_phases.apply_changes(conn, [reopen], "chair-loop") == 1
    assert [(r["round"], r["verdict"]) for r in store_phases.review_rounds(conn, "ship", "")] == [
        (1, "revise"), (2, review_loop.REOPENED)]


def _revise_change(**task):
    return {"op": ir.REVISE_OP, "initiative": "ship", "phase": "revise-1",
            "task": {"task_id": "revise-1", "kind": "task", "phase": "revise-1", "state": "ready", "needs": [],
                     "title": "Revise ship", "surfaces": ["lib/copy.py"], "body": "Delete lib/copy.py.", **task},
            "row": {"initiative": "ship", "phase": "revise-1", "kind": "build", "repo": "~/src/app",
                    "base_ref": "init/ship", "requires": ["repo:app"], "band": 1, "seq": 3}}


def test_a_revision_phase_is_its_task_row_and_its_build_row_cut_from_the_initiative_branch_once(conn):
    assert store_api.phase_changes_on(conn, [_revise_change()], "chair-loop") == 2
    (task,) = [r for r in read(conn, "ship") if r["task_id"] == "revise-1"]
    assert (task["phase"], task["state"], task["title"], task["body"]) == ("revise-1", "ready", "Revise ship", "Delete lib/copy.py.")
    row = store_phases.get_row(conn, "ship", "revise-1")
    assert (row["kind"], row["base_ref"], row["state"], row["requires_json"]) == ("build", "init/ship", "ready", ["repo:app"])
    # A repeat, even one that would say something else, writes nothing: the first write stands.
    assert store_api.phase_changes_on(conn, [_revise_change(title="Another")], "chair-loop") == 0
    assert [r["title"] for r in read(conn, "ship") if r["task_id"] == "revise-1"] == ["Revise ship"]


# -- the checks -------------------------------------------------------------------------------------------------------

CHECKS = """\
cd src/tools && pytest -q -n auto
# level: initiative
cd src/tools && pytest -q -n auto
pytest -q -rs smoke
pytest -q -n auto  # new model only
"""


def test_a_new_model_only_line_runs_at_its_level_and_the_old_model_s_list_leaves_it_out():
    parsed = repo_checks(CHECKS)
    whole = for_level(parsed, "initiative")
    assert [c["cmd"] for c in whole] == ["cd src/tools && pytest -q -n auto", "pytest -q -rs smoke", "pytest -q -n auto"]
    assert whole[-1][NEW_MODEL_ONLY] is True and whole[-1]["name"] == "pytest -q -n auto"
    assert NEW_MODEL_ONLY not in whole[0]
    assert [c["cmd"] for c in old_model_checks(parsed)] == ["cd src/tools && pytest -q -n auto", "pytest -q -rs smoke"]


def test_the_suite_reads_the_tree_s_own_file(tmp_path):
    landing = {"ticket_checks": [{"name": "gate", "cmd": "sh gate.sh"}]}
    assert [c["cmd"] for c in tree_gate_checks(landing, "ticket", tmp_path)] == ["sh gate.sh"]  # no file: the list alone
    (tmp_path / ".agent-checks").write_text(CHECKS, encoding="utf-8")
    assert [c["cmd"] for c in tree_gate_checks(landing, "ticket", tmp_path)] == [
        "sh gate.sh", "cd src/tools && pytest -q -n auto", "pytest -q -rs smoke", "pytest -q -n auto"]
    (tmp_path / ".agent-checks").write_bytes(b"\xff\xfe\x00bad")
    assert [c["cmd"] for c in tree_gate_checks(landing, "ticket", tmp_path)] == ["sh gate.sh"]


# -- the graph's ticket -----------------------------------------------------------------------------------------------

def test_an_initiative_round_is_asked_of_the_whole_initiative_and_a_phase_round_is_unchanged():
    args = {"phase": ir.QUEUE_PHASE, "initiative": "ship", "round": 2, "goal": "initiative 'ship': Ship",
            "previous": [{"round": 1, "verdict": "revise", "reason": "dedupe"}], "scope": "initiative"}
    text = phase_review._ticket(args)
    assert text.startswith("Initiative ship as a whole, review round 2.")
    assert "THE INITIATIVE'S GOAL" in text and "earlier rounds of this initiative said" in text
    assert "Review the whole initiative, not one phase" in text and "Review the whole phase" not in text
    assert "the reason the initiative is held" in text
    phase = phase_review._ticket({**args, "phase": "p1", "scope": None})
    assert phase.startswith("Phase p1 of initiative ship, review round 2.") and phase_review.QUESTIONS in phase
