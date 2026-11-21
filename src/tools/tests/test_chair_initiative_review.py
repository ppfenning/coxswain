"""D2, the chair's side of the initiative review, pure: when the review is queued, the revision phase a `revise` round
has it write, the same review queued again once that phase has merged, and the tick action that carries them."""

from agent_tools import chair_initiative_review as cir
from agent_tools.chair_plan import plan_tick
from harness import initiative_review as ir
from harness import review_loop
from tests.test_chair_phase_queue import NOW, _queue, _stored
from tests.test_chair_plan import _facts as _tick_facts

BRANCH = "init/ship"


def _record(*phases: str, lane_model: str | None = "phase", **extra) -> dict:
    return {"initiative": "ship", "repo": "~/src/app", "branch": BRANCH, "parent": "main", "state": "building",
            "phases": {p: {"state": "merged", "tip": f"{p}-tip"} for p in phases},
            **({"lane_model": lane_model} if lane_model else {}), **extra}


def _tasks(*rows: tuple[str, str, str]) -> list[dict]:
    return [{"initiative": "ship", "task_id": t, "phase": p, "state": s, "merged_commit": ""} for t, p, s in rows]


DONE = _tasks(("t1", "p1", "approved"), ("t2", "p2", "approved"))
PHASES = [_stored("p1", state="finished", outcome="approved", seq=0), _stored("p2", state="finished", outcome="approved", seq=1)]


def _review(state: str, outcome: str = "", epoch: int = 3) -> dict:
    return {**_stored(ir.QUEUE_PHASE, state=state, outcome=outcome, epoch=epoch, seq=2), "kind": ir.KIND}


def _changes(records, tasks=DONE, stored=PHASES, rounds=None, withheld=()):
    return cir.review_changes(records, tasks, stored, rounds or {}, lambda repo: ["docker", f"repo:{repo.rpartition('/')[2]}"],
                              withheld)


def _rounds(*verdicts: str, revision: dict | None = None) -> dict:
    return {"ship": [{"round": n, "verdict": v, "reason": f"r{n}", "head_sha": f"h{n}",
                      "revision_json": (revision or {}) if v == "revise" else {}, "cost_usd": 0.0}
                     for n, v in enumerate(verdicts, 1)]}


def test_an_assembled_new_model_initiative_gets_one_review_row_after_its_phases():
    (insert,) = _changes([_record("p1", "p2")])
    assert {k: insert[k] for k in ("op", "initiative", "phase", "kind", "repo", "base_ref", "requires", "seq")} == {
        "op": "insert", "initiative": "ship", "phase": ir.QUEUE_PHASE, "kind": ir.KIND, "repo": "~/src/app",
        "base_ref": BRANCH, "requires": ["docker", "repo:app"], "seq": 2}
    assert _changes([_record("p1", "p2")], stored=[*PHASES, _review("ready")]) == []  # queued once


def test_nothing_is_queued_before_every_phase_merged_on_the_old_model_or_while_held():
    assert _changes([_record("p1")]) == []  # p2 has not merged
    assert _changes([_record("p1", "p2", lane_model=None)]) == []
    assert _changes([_record("p1", "p2", held={"reason": "drift", "since": "x"})]) == []
    assert _changes([_record("p1", "p2")], withheld={"ship"}) == []
    assert _changes([_record("p1", "p2", state="pr_open")]) == []  # its pull request is open already


def test_a_revise_writes_its_revision_phase_once_then_queues_the_review_again_once_it_merged():
    revision = ir.revision_task("ship", 1, {"phase": ir.QUEUE_PHASE, "reason": "dedupe",
                                            "review": {"findings": [{"file": "lib/copy.py", "detail": "delete it"}]}})
    stored = [*PHASES, _review("finished", "revised")]
    rounds = _rounds("revise", revision=revision)
    (write,) = _changes([_record("p1", "p2")], stored=stored, rounds=rounds)
    assert (write["op"], write["phase"]) == (ir.REVISE_OP, "revise-1")
    assert write["task"]["task_id"] == "revise-1" and write["task"]["phase"] == "revise-1"
    assert write["task"]["title"] == "Revise ship after initiative review round 1" and "- lib/copy.py: delete it" in write["task"]["body"]
    assert write["task"]["surfaces"] == ["lib/copy.py"] and write["task"]["state"] == "ready"
    assert {k: write["row"][k] for k in ("phase", "kind", "base_ref", "requires", "seq")} == {
        "phase": "revise-1", "kind": "build", "base_ref": BRANCH, "requires": ["docker", "repo:app"], "seq": 2}
    building = [*DONE, *_tasks(("revise-1", "revise-1", "ready"))]
    assert _changes([_record("p1", "p2")], building, stored, rounds) == []  # written, not merged: nothing more
    merged = [*DONE, *_tasks(("revise-1", "revise-1", "approved"))]
    (again,) = _changes([_record("p1", "p2", "revise-1")], merged, stored, rounds)
    assert (again["op"], again["phase"], again["epoch"]) == (ir.REREVIEW_OP, ir.QUEUE_PHASE, 3)
    assert "round 2 reviews it" in again["reason"]


def test_a_finished_review_is_the_lander_s_and_a_withdrawn_one_comes_back_once_nothing_holds_it():
    for outcome in ("approved", "quarantined", "failed"):
        assert _changes([_record("p1", "p2")], stored=[*PHASES, _review("finished", outcome)], rounds=_rounds("approve")) == []
    (back,) = _changes([_record("p1", "p2")], stored=[*PHASES, _review("cancelled")])
    assert (back["op"], back["phase"], back["epoch"]) == ("reopen", ir.QUEUE_PHASE, 3)


def _moved(**extra) -> dict:
    return _record("p1", "p2", review_tip={"tip": "h-new", "moves": 1}, **extra)


def test_a_finished_review_runs_again_once_the_branch_moved_from_the_tip_its_last_round_reviewed():
    for outcome, verdict in (("approved", "approve"), ("failed", "approve"), ("quarantined", "quarantine")):
        stored = [*PHASES, _review("finished", outcome)]
        rounds = _rounds(verdict)
        found = [c for c in _changes([_moved()], stored=stored, rounds=rounds) if c["op"] == "reopen"]
        assert len(found) == 1 and (found[0]["phase"], found[0]["epoch"]) == (ir.QUEUE_PHASE, 3), (outcome, found)
        assert "moved from h1" in found[0]["reason"] and "h-new" in found[0]["reason"]
        # At the tip it reviewed, before the gate read a tip, once held or no longer assembled, it is left alone.
        assert not [c for c in _changes([_record("p1", "p2", review_tip={"tip": "h1"})], stored=stored, rounds=rounds)
                    if c["op"] == "reopen"]
        assert not [c for c in _changes([_record("p1", "p2")], stored=stored, rounds=rounds) if c["op"] == "reopen"]
        assert not [c for c in _changes([_moved(held={"reason": "x", "since": "t"})], stored=stored, rounds=rounds)
                    if c["op"] == "reopen"]
        assert not [c for c in _changes([_moved(state="pr_open")], stored=stored, rounds=rounds) if c["op"] == "reopen"]
    # A reopened episode has no round yet: no tip reviewed, so it is not reopened again.
    closed = {"ship": [*_rounds("approve")["ship"], {"round": 2, "verdict": review_loop.REOPENED, "head_sha": ""}]}
    assert _changes([_moved()], stored=[*PHASES, _review("finished", "approved")], rounds=closed) == []


def test_a_quarantined_review_is_sent_to_triage_once_as_a_marked_quarantined_task():
    stored = [*PHASES, {**_review("finished", "quarantined"), "run_id": "ship.initiative-1"}]
    rounds = _rounds("quarantine")
    review_hold = {"reason": "the initiative review holds the pull request: q", "since": "t", "pull_request_only": True}
    (write,) = _changes([_record("p1", "p2", held=review_hold)], stored=stored, rounds=rounds)
    assert (write["op"], write["phase"], "row" in write) == (ir.REVISE_OP, "fix-1", False)
    task = write["task"]
    assert (task["task_id"], task["phase"], task["state"]) == ("fix-1", "fix-1", "quarantined")
    assert task["extra"]["quarantine_mark"] == {"cause": review_loop.REVIEW_CAUSE, "reason": "r1", "run": "ship.initiative-1"}
    assert "r1" in task["body"]
    triaged = [*DONE, *_tasks(("fix-1", "fix-1", "quarantined"))]
    assert _changes([_record("p1", "p2", held=review_hold)], triaged, stored, rounds) == []  # written once
    drift = {"reason": "drift", "since": "t"}
    assert _changes([_record("p1", "p2", held=drift)], stored=stored, rounds=rounds) == []  # a drift holds merges too
    assert _changes([_record("p1", "p2")], stored=stored, rounds=rounds, withheld={"ship"}) == []


def test_the_tick_queues_the_review_and_its_revision_in_its_queue_phases_action():
    records = [_record("p1", "p2")]
    queue = {**_queue(PHASES, phase_initiatives=["ship"]), "tasks": DONE, "records": records, "initiative_rounds": {}}
    (action,) = [a for a in plan_tick({**_tick_facts(initiatives=[]), "phase_queue": queue}, NOW) if a["kind"] == "queue_phases"]
    assert [(c["op"], c["phase"]) for c in action["changes"]] == [("insert", ir.QUEUE_PHASE)]
    held = {**queue, "rows": [*PHASES, _review("finished", "revised")],
            "initiative_rounds": _rounds("revise", revision={"title": "Revise", "body": "b"})}
    (action,) = [a for a in plan_tick({**_tick_facts(initiatives=[]), "phase_queue": held}, NOW) if a["kind"] == "queue_phases"]
    assert [(c["op"], c["phase"]) for c in action["changes"]] == [(ir.REVISE_OP, "revise-1")]
    assert "ship/revise-1 revise_initiative" in action["reason"]


def test_a_review_row_of_a_held_initiative_is_withdrawn_like_any_row():
    queue = {**_queue([*PHASES, _review("ready")], phase_initiatives=["ship"]), "tasks": DONE,
             "records": [_record("p1", "p2")], "initiative_rounds": {}}
    facts = {**_tick_facts(initiatives=[]), "phase_queue": queue, "held_initiatives": ["ship"]}
    (action,) = [a for a in plan_tick(facts, NOW) if a["kind"] == "queue_phases"]
    assert [(c["op"], c["phase"]) for c in action["changes"]] == [("withdraw", ir.QUEUE_PHASE)]


def test_only_the_current_episode_s_revise_asks_for_a_revision():
    stored = [*PHASES, _review("finished", "revised")]
    closed = {"ship": [*_rounds("revise")["ship"], {"round": 2, "verdict": review_loop.REOPENED, "reason": "",
                                                    "head_sha": "", "revision_json": {}, "cost_usd": 0.0}]}
    assert _changes([_record("p1", "p2")], stored=stored, rounds=closed) == []
