"""A task failing while the run is terminating records no attempt, so the next launch builds it."""

from __future__ import annotations

import pytest

from core import workstore
from harness import termination
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    builds_per_task,
    cart,
    drive,
    new_file_patch,
    repo,
)


@pytest.fixture(autouse=True)
def _flag_clear():
    termination._TERMINATING.clear()
    yield
    termination._TERMINATING.clear()


def _one_task(tmp_path):
    wi = tmp_path / "wi"
    (wi / "p1-foundations").mkdir(parents=True)
    (wi / "initiative.md").write_text("---\nid: demo-initiative\ntitle: demo\n---\n\nmake the join measurable\n")
    path = wi / "p1-foundations" / "t1-probe.md"
    path.write_text(
        "---\nid: t1-probe\nphase: p1-foundations\nstate: ready\nneeds: []\nsurfaces: []\n"
        "title: schema probe\n---\n\nread the vendor schema\n"
    )
    return wi, path


def test_a_build_failing_under_termination_records_no_attempt_and_stays_ready(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, path = _one_task(tmp_path)
    termination.mark_terminating()
    result, runner = drive(repo, cart, tmp_path, runner=Runner({}), work=workstore.read_initiative(wi))

    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert entry["reason"].startswith("run terminated by signal; not recorded: ")
    item = workstore.read_item(path)
    assert (item["state"], item.get("attempts") or []) == ("ready", [])
    assert not any(call["role"] == "triage" for call in runner.calls)


def test_the_same_failure_with_the_flag_clear_records_one_no_work_attempt(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, path = _one_task(tmp_path)
    drive(repo, cart, tmp_path, runner=Runner({}), work=workstore.read_initiative(wi))

    assert [a["kind"] for a in workstore.read_item(path)["attempts"]] == ["no_work"]


def test_a_fresh_run_after_a_terminated_one_builds_the_task(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, path = _one_task(tmp_path)
    termination.mark_terminating()
    drive(repo, cart, tmp_path, runner=Runner({}), work=workstore.read_initiative(wi), run_id="epic-killed")
    termination._TERMINATING.clear()
    runner = Runner({"t1-probe": new_file_patch("t1-probe.txt")})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=workstore.read_initiative(wi), run_id="epic-fresh")

    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])
    assert not any(call["role"] == "triage" for call in runner.calls)
    assert builds_per_task(runner)["t1-probe"] == 1
