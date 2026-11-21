"""A phase-model initiative's landing record is made by the lander's own pass, with no `cox lander adopt`.

E1, 2026-11-21: `seat-context-string-resolves-intact-not-as-a` finished its phase p1 approved and nothing happened: the
new model has no epic run, and only adopt made a record, so the lander never merged the phase. The record is made the
moment the chair queues a phase of the initiative (`lander_adopt.record_now`), through adopt's own path.

The pure decisions first, with no mocks. Then the lander over a real repo, a real bare origin and a real store
(`Models`): the phase queue rows are the chair's own writes (`store_phases.apply_changes`).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_tools import lander, lander_adopt
from core import landing_record as lr
from harness import landing_store as ls
from harness import store_phases
from tests.test_lander import run, task
from tests.test_lander_models import Models

# -- the pure core --------------------------------------------------------------------------------------------------

PHASE = {"initiative": "n", "phase": "p1", "kind": "phase", "state": "ready"}


@pytest.mark.parametrize(("model", "rows", "found", "want"), [
    ("phase", [PHASE], None, True),
    ("phase", [{**PHASE, "state": "claimed"}], None, True),
    ("phase", [{**PHASE, "state": "finished"}], None, True),  # the first phase already finished: made now, late
    ("phase", [{**PHASE, "kind": ""}], None, True),  # a row written before kinds is a phase
    ("phase", [{**PHASE, "state": "cancelled"}], None, False),  # withdrawn: held, closed or over budget
    ("phase", [{**PHASE, "state": "cancelled"}, {**PHASE, "phase": "p2"}], None, True),
    ("phase", [{**PHASE, "phase": "initiative", "kind": "initiative_review"}], None, False),
    ("phase", [], None, False),  # the chair has queued nothing yet
    ("task", [PHASE], None, False),
    (None, [PHASE], None, False),
    ("Phase", [PHASE], None, False),
    ("phase", [PHASE], {"initiative": "n"}, False),  # it has a record
    ("phase", [PHASE], lr.Unreadable("n", "bad json"), False),  # a row that does not read is left for a person
])
def test_the_lander_makes_the_record_once_the_chair_queues_a_phase_of_a_phase_model_initiative_with_none(
        model, rows, found, want):
    assert lander_adopt.record_now(model, rows, found) is want


def _plan(*actions: str, skip: str = "") -> lander_adopt.Plan:
    found = tuple(lander_adopt.TaskFinding(f"t{i}", "p1", a) for i, a in enumerate(actions))
    return lander_adopt.Plan("n", skip=skip, branch="init/n", parent="main", tasks=found)


def test_a_plan_that_moves_no_approved_work_is_adopted_unasked_and_any_other_is_left_to_a_person():
    assert lander_adopt.left_to_a_person(_plan()) is None
    assert lander_adopt.left_to_a_person(_plan("none", "keep", "done")) is None
    assert lander_adopt.left_to_a_person(_plan(skip="it lands at task")) == "it lands at task"
    why = lander_adopt.left_to_a_person(_plan("keep", "ready", "carry"))
    assert why == ("adopting it would move approved work: t1 (back to ready), t2 (carried onto its phase branch); "
                   "check it, then run `cox lander adopt --apply n`")


# -- the lander, against a real store -------------------------------------------------------------------------------

@pytest.fixture
def models(tmp_path: Path) -> Models:
    return Models(tmp_path)


def _phase_model(models: Models, initiative: str, tasks: list[dict], *, lane_model: str = "phase") -> None:
    models.rows[initiative] = tasks
    models.meta_of[initiative] = {"repo": str(models.repo), "lane_model": lane_model}
    models.ports = replace(models.ports, repo=lambda record: models.repo)


def _queued(models: Models, initiative: str, *phases: str) -> None:
    """The chair's phase-queue step: one ready row per phase."""
    store_phases.apply_changes(models.conn, [{"op": "insert", "initiative": initiative, "phase": p, "requires": []}
                                             for p in phases], "chair")


def _records(models: Models) -> list[str]:
    return [str(r["initiative"]) for r in ls.list_records(models.conn).records]


def _approved(models: Models, initiative: str, phase: str, tip: str) -> None:
    """The chair's `approve_on`: every task of the phase approved at the lane's roll-up commit."""
    for row in models.rows[initiative]:
        if row["phase"] == phase:
            row["merged_commit"] = tip


def test_a_queued_phase_model_initiative_gets_one_record_with_no_adopt_and_its_approved_phase_merges(models: Models):
    _phase_model(models, "n", [task("a", "p1"), task("b", "p2")])
    _queued(models, "n", "p1", "p2")
    assert lander.run_once(models.ports) == []
    record = models.record("n")
    assert (record["lane_model"], record["adopted"], record["branch"], record["phases"]) == ("phase", False, "init/n", {})
    assert models.on_origin("init/n") == record["base_sha"] == run(models.repo, "rev-parse", "main")
    assert "n: record made on init/n" in models.logged

    tip = models.lane("n", "p1", "x.txt", "x\n")  # cut from origin's init/n, which the lander pushed
    _approved(models, "n", "p1", tip)
    outcomes = lander.run_once(models.ports)
    assert [(o.item, o.status) for o in outcomes] == [("phase_merge:n:p1", "done")], outcomes
    assert models.record("n")["phases"]["p1"]["tip"] == tip
    assert run(models.origin, "merge-base", "--is-ancestor", tip, "init/n") == ""
    for _ in range(2):
        lander.run_once(models.ports)
    assert _records(models) == ["n"] and models.record("n")["created_at"] == record["created_at"]
    assert models.notes == []


def test_a_first_phase_approved_before_the_lander_saw_it_is_recorded_and_merged_in_the_same_pass(models: Models):
    _phase_model(models, "n", [task("a", "p1")])
    run(models.repo, "push", "-q", "origin", "main:refs/heads/init/n")  # where the lane cut it from
    tip = models.lane("n", "p1", "x.txt", "x\n")
    _approved(models, "n", "p1", tip)
    _queued(models, "n", "p1")
    outcomes = lander.run_once(models.ports)
    assert [(o.item, o.status, o.detail) for o in outcomes] == [
        ("phase_merge:n:p1", "done", "phase p1 of n merged into init/n"),
        ("initiative_land:n", "waiting", "the pull request waits: the initiative review has not decided a round yet"),
    ], outcomes
    assert models.record("n")["phases"]["p1"]["state"] == "merged" and _records(models) == ["n"]


def test_an_initiative_with_a_record_an_old_model_one_and_one_the_chair_has_not_queued_are_left_alone(models: Models):
    models.start_new("kept", [task("a", "p1")])
    before = models.record("kept")
    _queued(models, "kept", "p1")
    _phase_model(models, "old", [task("a", "p1")], lane_model="task")
    _queued(models, "old", "p1")
    _phase_model(models, "later", [task("a", "p1")])
    lander.run_once(models.ports)
    assert _records(models) == ["kept"] and models.record("kept") == before


def test_approved_work_origin_does_not_hold_is_left_to_a_person_and_the_chair_is_told_once(models: Models):
    _phase_model(models, "n", [{**task("a", "p1", state="approved"), "merged_commit": "d" * 40}])
    _queued(models, "n", "p1")
    for _ in range(2):
        lander.run_once(models.ports)
    assert _records(models) == [] and [r["state"] for r in models.rows["n"]] == ["approved"], "nothing was sent back"
    [(topic, note)] = models.notes
    assert topic == "needs_chair" and note["reason"].startswith("adopting it would move approved work: a (back to ready)")
    assert sum("no landing record is made on its own" in line for line in models.logged) == 1
