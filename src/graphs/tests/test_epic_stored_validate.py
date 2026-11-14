"""The phase validator reads every stored approved task of the phase, not only what this run built."""

from __future__ import annotations

import pytest

from harness import epic as epic_module
from tests.test_epic_driver import (  # noqa: F401
    GOAL_UNMET,
    Runner,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)

P1, P2 = "p1-foundations", "p2-rollout"
STALE = {"state": "approved", "merged": True, "run_id": "epic-0"}
HANDOFF = {"files_changed": ["t1-probe.txt"], "interfaces_added": ["probe()"], "decisions": ["read only"]}


def _stored(work: dict, **by_id: dict) -> dict:
    """`work` with the named items replaced by their stored rows."""
    return {**work, "items": [{**i, **by_id[i["id"]]} if i["id"] in by_id else i for i in work["items"]]}


def _phase(result: dict, phase: str) -> dict:
    return next(p for p in result["phases"] if p["phase"] == phase)


def _builds(runner: Runner, task: str) -> bool:
    return any(c["role"] == "build" and task in c["prompt"] for c in runner.calls)


@pytest.fixture
def validator_inputs(monkeypatch) -> list[dict]:
    """The `phase_state` each validate_phase invocation was handed."""
    seen: list[dict] = []
    real = epic_module.invoke_graphs

    def spy(invocations, **kwargs):
        seen.extend(i.args["phase_state"] for i in invocations if "phase_state" in i.args)
        return real(invocations, **kwargs)

    monkeypatch.setattr(epic_module, "invoke_graphs", spy)
    return seen


def _one_phase_with_two_stored() -> dict:
    """p1 holds t1 and t2 approved earlier, one with a handoff and one without, and t3 ready to build."""
    work = initiative(two_phases=False)
    t3 = {"id": "t3-cutover", "phase": P1, "state": "ready", "needs": [], "surfaces": [], "title": "cutover", "body": "move"}
    work = {**work, "items": [*work["items"], t3]}
    return _stored(work, **{"t1-probe": {**STALE, "handoff": HANDOFF}, "t2-bench": {**STALE, "handoff": None}})


def test_first_phase_approved_in_the_store_completes_and_the_second_phase_builds(repo, cart, tmp_path) -> None:  # noqa: F811
    work = _stored(initiative(), **{"t1-probe": STALE, "t2-bench": STALE})
    result, runner = drive(repo, cart, tmp_path, work=work, run_id="epic-1")
    assert _phase(result, P1)["status"] == "complete"
    assert _builds(runner, "t3-cutover")
    assert not _builds(runner, "t1-probe") and not _builds(runner, "t2-bench")


def test_a_quarantined_first_phase_task_leaves_it_partial_and_blocks_the_second(repo, cart, tmp_path) -> None:  # noqa: F811
    work = _stored(initiative(), **{"t1-probe": STALE, "t2-bench": {"state": "quarantined"}})
    result, runner = drive(repo, cart, tmp_path, work=work, run_id="epic-1")
    assert _phase(result, P1)["status"] == "partial"
    assert _phase(result, P2)["status"] == "blocked"
    assert "did not meet its goal" in _phase(result, P2)["reason"]
    assert not _builds(runner, "t3-cutover")


def test_the_validator_runs_once_over_every_stored_task_and_the_phase_completes(
    repo, cart, tmp_path, validator_inputs  # noqa: F811
) -> None:
    runner = Runner({"t3-cutover": new_file_patch("t3-cutover.txt")})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=_one_phase_with_two_stored())
    assert len(validator_inputs) == 1
    entries = {t["id"]: t for t in validator_inputs[0]["tasks"]}
    assert set(entries) == {"t1-probe", "t2-bench", "t3-cutover"}
    assert len({frozenset(e) for e in entries.values()}) == 1
    assert entries["t1-probe"]["change_facts"]["files_changed"] == ["t1-probe.txt"]
    assert entries["t2-bench"]["change_facts"] == {"files_changed": [], "interfaces_added": [], "decisions": []}
    assert entries["t2-bench"]["evidence"] == [] and entries["t2-bench"]["patch"] == ""
    record = _phase(result, P1)
    assert "skipped" not in record["phase_verdict"]
    assert record["status"] == "complete"


def test_an_unbound_validator_over_the_same_shape_still_reads_partial(
    repo, cart, tmp_path, validator_inputs  # noqa: F811
) -> None:
    del cart["skills"]["validate_phase"]
    runner = Runner({"t3-cutover": new_file_patch("t3-cutover.txt")})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=_one_phase_with_two_stored())
    assert validator_inputs == []
    assert (_phase(result, P1)["status"], _phase(result, P1)["reason"]) == ("partial", "no validator bound")


def test_an_unmet_goal_over_stored_tasks_reads_partial(repo, cart, tmp_path) -> None:  # noqa: F811
    runner = Runner({"t3-cutover": new_file_patch("t3-cutover.txt")}, verdicts={P1: GOAL_UNMET})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=_one_phase_with_two_stored())
    assert _phase(result, P1)["status"] == "partial"
