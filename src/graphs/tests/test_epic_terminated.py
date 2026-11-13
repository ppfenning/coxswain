"""A build that fails while the run is being killed is the operator's failure: no attempt, task stays ready."""

from __future__ import annotations

import pytest

from core import workstore
from harness import termination
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    FailOnceRunner,
    _one_task_initiative,
    cart,
    drive,
    is_ancestor,
    new_file_patch,
    repo,
)

PATCHES = {"t1-probe": new_file_patch("t1-probe.txt")}
BRANCH = "epic/demo-initiative/p1-foundations--t1-probe"


@pytest.fixture(autouse=True)
def _flag_clear():
    termination._TERMINATING.clear()
    yield
    termination._TERMINATING.clear()


def test_a_failing_build_under_termination_records_no_attempt_and_stays_ready(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, work = _one_task_initiative(tmp_path)
    termination.mark_terminating()
    runner = FailOnceRunner(PATCHES, message="provider-side safeguard error")
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-kill-1")

    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert entry["kind"] == "no_work"
    assert entry["reason"].startswith("run terminated by signal; not recorded:")
    item = workstore.read_item(wi / "p1-foundations" / "t1-probe.md")
    assert not item.get("attempts")
    assert item["state"] == "ready"
    assert not any(call["role"] == "cause_classify" for call in runner.calls)


def test_the_same_failure_with_the_flag_clear_records_one_no_work_attempt(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, work = _one_task_initiative(tmp_path)
    runner = FailOnceRunner(PATCHES, message="provider-side safeguard error")
    drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-kill-1")

    item = workstore.read_item(wi / "p1-foundations" / "t1-probe.md")
    assert [a["kind"] for a in item["attempts"]] == ["no_work"]


def test_a_fresh_run_after_the_killed_one_builds_the_task_rather_than_triaging_it(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, work = _one_task_initiative(tmp_path)
    runner = FailOnceRunner(PATCHES, message="provider-side safeguard error")
    termination.mark_terminating()
    drive(repo, cart, tmp_path, runner=runner, work=work, run_id="epic-kill-1")
    termination._TERMINATING.clear()

    result, runner = drive(repo, cart, tmp_path, runner=runner, work=workstore.read_initiative(wi), run_id="epic-kill-2")
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])
    assert not any(call["role"] == "triage" for call in runner.calls)
    assert is_ancestor(repo, BRANCH, "epic/demo-initiative/p1-foundations")
