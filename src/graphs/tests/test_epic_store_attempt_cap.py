"""Under work_state store the epic's attempt cap and its resume from the last attempt read the runs' task records (H4.3).

Nothing writes an attempt into a work file there, so a task the epic quarantined twice reached the next run with no
attempt: it was built a third time, and a rebuild started from nothing. These runs are real `run_epic` runs on a real
repository and one throwaway sqlite store: the quarantines and their task records are the epic's own writes, and only
the row's move back to `ready` between runs is done here, through the store's own state writer, as the chair moves it.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

from harness import epic, store_work_state
from harness.store_write import Store
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    REVISE,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)
from tests.test_epic_triage_classes import (
    INITIATIVE,
    TRIAGE_SPECS,
    TriageClassRunner,
    _answer,
    _built,
    _row,
    _store,
    _triage_calls,
)

PHASE = "p1-foundations"


def _revised(patch: str) -> TriageClassRunner:
    """Builds t1-probe with `patch`, and its reviewer says revise every time: the task is quarantined, its patch kept."""
    runner = TriageClassRunner({"t1-probe": patch}, _answer("hold"))
    runner.review = {"t1-probe": REVISE}
    return runner


def _work() -> dict:
    one = initiative(two_phases=False)
    return {**one, "items": [i for i in one["items"] if i["id"] == "t1-probe"]}


def _ready_again(store: Store) -> None:
    moved = store_work_state.set_state(
        store.conn, INITIATIVE, "t1-probe", "ready", "chair", PHASE, datetime.now(UTC).isoformat(), expected="quarantined"
    )
    assert not isinstance(moved, store_work_state.Mismatch), moved


def _run(repo, cart, tmp_path, store, n, runner):  # noqa: F811
    result, _ = drive(
        repo, cart, tmp_path, runner=runner, work=_work(), store=store, work_state="store", specs=TRIAGE_SPECS,
        run_id=f"{INITIATIVE}-{n}",
    )
    return result


def test_a_store_mode_task_quarantined_twice_is_triaged_not_built_a_third_time(repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store(_work(), flagged=())
    for n in (1, 2):
        result = _run(repo, cart, tmp_path, store, n, _revised(new_file_patch("t1-probe.txt")))
        assert [q["id"] for q in result["quarantined"]] == ["t1-probe"], result["quarantined"]
        assert _row(store, "t1-probe")["state"] == "quarantined"
        _ready_again(store)

    third = TriageClassRunner({"t1-probe": new_file_patch("t1-probe.txt")}, _answer("hold"))
    _run(repo, cart, tmp_path, store, 3, third)
    assert not _built(third, "t1-probe")
    (call,) = _triage_calls(third)
    assert f"{INITIATIVE}-1" in call["prompt"] and f"{INITIATIVE}-2" in call["prompt"]
    assert _row(store, "t1-probe")["state"] == "blocked"


def test_a_store_mode_rebuild_resumes_from_the_last_attempt_kept_only_in_the_store(repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store(_work(), flagged=())
    first = _revised(new_file_patch("t1-probe.txt"))
    assert [q["id"] for q in _run(repo, cart, tmp_path, store, 1, first)["quarantined"]] == ["t1-probe"]
    _ready_again(store)
    # Another lane ran it: this machine has no file of that run, only the store's copy of its task record.
    shutil.rmtree(tmp_path / "runs" / f"{INITIATIVE}-1")

    second = TriageClassRunner({"t1-probe": new_file_patch("t1-probe.txt")}, _answer("hold"))
    _run(repo, cart, tmp_path, store, 2, second)
    (build,) = _built(second, "t1-probe")
    assert "Your previous attempt is in the tree you are working in" in build["prompt"]
    assert f"{INITIATIVE}-1" in build["prompt"]


def test_with_record_attempts_fills_only_an_item_with_none_of_its_own() -> None:
    recorded = [{"run": "r-1", "reason": "x"}]
    assert epic.with_record_attempts({"id": "t"}, recorded) == {"id": "t", "attempts": recorded}
    own = {"id": "t", "attempts": [{"run": "r-0"}]}
    assert epic.with_record_attempts(own, recorded) is own
    bare = {"id": "t"}
    assert epic.with_record_attempts(bare, []) is bare

