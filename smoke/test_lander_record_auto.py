"""A phase-model initiative's landing record is made by the lander's own pass: no `cox lander adopt`, ever.

E1, 2026-11-21: on the new model nothing made the record (the old model's epic run does), so an approved phase was
never merged and `cox lander status` did not list the initiative until an operator ran adopt by hand. Here the chair
queues the initiative's phases, `cox lander once` makes the record (`lane_model: phase`) and pushes its branch, the
worker builds and approves both phases, the chair approves their tasks, and the next `cox lander once` merges both
into the initiative branch on origin and takes its next step: the pull request waits for the initiative review, which
the chair then queues at that branch. Real Postgres, the real `cox worker`, `cox chair` and `cox lander`; the scripted
runner stands in for the model. The machine is `test_initiative_review`'s, without its adopt.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from smoke.test_initiative_review import (
    INIT,
    INITIATIVE,
    _lander,
    _machine,
    _origin_tip,
    _phase_row,
    _phases_built_and_merged,
    _plan_answers,
    _queued_review,
)
from smoke.test_lander_lanes import _record
from smoke.test_phase_lane import APPROVE, _owner

pytest.importorskip("psycopg")


@pytest.fixture
def unadopted_machine(launchable, repo) -> Iterator[tuple]:
    for sandbox, name, env in _machine(launchable, repo.path, adopt=False):
        yield sandbox, name, env, launchable, repo.path


def _records(sandbox) -> list[str]:
    return [str(r[0]) for r in _owner(sandbox, "SELECT initiative FROM landing_records ORDER BY initiative")[0]]


def test_an_approved_phase_reaches_the_initiative_branch_and_the_lander_s_next_step_with_no_adopt(unadopted_machine):
    sandbox, name, env, launchable, clone = unadopted_machine
    launchable.scripted(_plan_answers(p1_review_1=APPROVE, p2_review_1=APPROVE))
    assert _phase_row(sandbox, "p2")["state"] == "ready" and _records(sandbox) == [], "the chair queued; no record yet"

    out = _lander(sandbox)
    assert f"{INITIATIVE}: record made on {INIT}" in out, out
    record = _record(sandbox, INITIATIVE)
    assert (record["lane_model"], record["adopted"], record["phases"]) == ("phase", False, {}), record
    assert _origin_tip(sandbox, clone, INIT) == record["base_sha"], "the lander pushed the branch the lanes cut from"
    status = sandbox.cox("lander", "status")
    assert status.returncode == 0 and INITIATIVE in status.stdout, status.stdout + status.stderr

    tip = _phases_built_and_merged(sandbox, env, name, clone)  # both merged; the pull request waits for the review
    assert tip != record["base_sha"] and _records(sandbox) == [INITIATIVE]
    assert {p: e["state"] for p, e in _record(sandbox, INITIATIVE)["phases"].items()} == {"p1": "merged", "p2": "merged"}
    _queued_review(sandbox)  # the chair queues the initiative review at the initiative branch

    again = _lander(sandbox)
    assert "record made" not in again and _records(sandbox) == [INITIATIVE], again
    assert _record(sandbox, INITIATIVE)["created_at"] == record["created_at"]
