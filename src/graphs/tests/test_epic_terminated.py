"""A task failing while the run is terminating records no attempt, so the next launch builds it."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import workstore
from harness import termination
from harness.cause_model import ROLE as CAUSE_ROLE
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    builds_per_task,
    cart,
    drive,
    new_file_patch,
    repo,
)

REASON_PREFIX = "run terminated by signal; not recorded:"


@pytest.fixture(autouse=True)
def _clear_termination_flag():
    """Nothing in the product clears the flag inside a process, so the tests do."""
    termination._TERMINATING.clear()
    yield
    termination._TERMINATING.clear()


def _store_with_one_ready_task(tmp_path: Path) -> tuple[Path, Path]:
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
    wi, path = _store_with_one_ready_task(tmp_path)
    termination.mark_terminating()
    result, runner = drive(repo, cart, tmp_path, runner=Runner({}), work=workstore.read_initiative(wi))

    item = workstore.read_item(path)
    assert not item.get("attempts")
    assert item["state"] == "ready"
    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert entry["kind"] == "no_work"
    assert entry["reason"].startswith(REASON_PREFIX)
    assert not any(call["role"] == CAUSE_ROLE for call in runner.calls)


def test_the_same_failure_with_the_flag_clear_records_one_no_work_attempt(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, path = _store_with_one_ready_task(tmp_path)
    result, _ = drive(repo, cart, tmp_path, runner=Runner({}), work=workstore.read_initiative(wi))

    attempts = workstore.read_item(path)["attempts"]
    assert [a["kind"] for a in attempts] == ["no_work"]
    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert not entry["reason"].startswith(REASON_PREFIX)


def test_a_fresh_run_after_a_terminated_run_builds_the_task(repo, cart, tmp_path) -> None:  # noqa: F811
    wi, path = _store_with_one_ready_task(tmp_path)
    termination.mark_terminating()
    drive(repo, cart, tmp_path, runner=Runner({}), work=workstore.read_initiative(wi))
    termination._TERMINATING.clear()

    runner = Runner({"t1-probe": new_file_patch("t1-probe.txt")})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=workstore.read_initiative(wi), run_id="epic-2")

    assert builds_per_task(runner)["t1-probe"] == 1
    assert not any(call["role"] == "triage" for call in runner.calls)
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])
