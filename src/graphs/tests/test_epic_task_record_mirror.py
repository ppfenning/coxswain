import logging
from dataclasses import replace
from pathlib import Path

import pytest

import harness.store_read as read
from harness.epic import Ctx, _task_record_mirror, save_task_result
from harness.epic_records import record_steps
from harness.resume import load_result, result_path
from harness.store_steps import steps_for, with_checks
from harness.store_write import Store

RESULT = {"ticket": "t1", "initiative": "i", "phase": "p1", "proposals": [{"kind": "draft_pr_create"}], "n": 1}


def ctx(tmp_path: Path, store) -> Ctx:
    return Ctx(
        repo=tmp_path,
        cartridge={},
        runner=None,
        specs={},
        run_id="r1",
        date="2026-09-25",
        max_parallel=1,
        ledger_path=tmp_path / "ledger",
        provider_profile="p",
        runs_dir=tmp_path / "runs",
        worktree_root=tmp_path / "wt",
        assume=None,
        fix_attempts=None,
        initiative_id="i",
        default_ref="main",
        store=store,
    )


class RaisingStore:
    def record_task_record(self, *args, **kwargs):
        raise RuntimeError("store is down")


def test_no_store_means_nothing_to_mirror():
    assert _task_record_mirror(None, RESULT, "r1", "p1", "t1", "ts") is None


def test_a_store_yields_the_upsert_arguments_with_the_record_as_the_file_holds_it():
    result = {"path": Path("/x"), "n": 1}
    args = _task_record_mirror(object(), result, "r1", "p1", "t1", "ts")
    assert args == ("r1", "p1", "t1", {"path": "/x", "n": 1}, "ts")


def test_the_row_exists_and_matches_the_file_and_a_second_save_replaces_it(tmp_path, store_conn):
    c = ctx(tmp_path, Store(store_conn))
    save_task_result(c, RESULT, phase="p1", task="t1")
    assert result_path(c.runs_dir, "r1", "p1", "t1").exists()
    on_file = load_result(c.runs_dir, "r1", "p1", "t1")
    assert on_file == RESULT
    assert read.task_record(store_conn, "r1", "p1", "t1") == on_file
    save_task_result(c, {**RESULT, "n": 2}, phase="p1", task="t1")
    assert read.task_records(store_conn, "r1") == {("p1", "t1"): {**RESULT, "n": 2}}


def test_a_raising_store_leaves_the_file_logs_a_warning_and_raises_nothing(tmp_path, caplog):
    c = ctx(tmp_path, RaisingStore())
    with caplog.at_level(logging.WARNING, logger="harness.epic"):
        save_task_result(c, RESULT, phase="p1", task="t1")
    assert load_result(c.runs_dir, "r1", "p1", "t1") == RESULT
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "harness.epic"]
    assert len(warnings) == 1
    assert "r1/p1/t1" in warnings[0].getMessage()
    assert "store is down" in warnings[0].getMessage()


def test_with_no_store_the_file_is_written_and_nothing_is_logged(tmp_path, caplog):
    c = ctx(tmp_path, None)
    with caplog.at_level(logging.WARNING):
        save_task_result(c, RESULT, phase="p1", task="t1")
    assert load_result(c.runs_dir, "r1", "p1", "t1") == RESULT
    assert caplog.records == []


@pytest.mark.parametrize("bad", [OSError("disk"), ValueError("bad")])
def test_any_store_exception_type_is_caught(tmp_path, bad):
    class Boom:
        def record_task_record(self, *a):
            raise bad

    save_task_result(ctx(tmp_path, Boom()), RESULT, phase="p1", task="t1")


LIFECYCLE = {
    **RESULT,
    "run_id": "r1:p1:t1",
    "plan": {"steps": ["s"], "files_expected": [], "out_of_scope": []},
    "build": {"patch": "--- a\n+++ b\n", "files_touched": ["NOTE.md"]},
    "review": {"verdict": "approve", "findings": []},
    "fix_loop": {"attempts": 1},
}
CHECKED = with_checks(LIFECYCLE, [{"name": "note", "passed": True, "exit_code": 0}], "r1")


def test_each_save_writes_the_results_steps_once_and_the_checks_when_a_later_save_carries_them(tmp_path, store_conn):
    c = ctx(tmp_path, Store(store_conn))
    save_task_result(c, LIFECYCLE, phase="p1", task="t1")
    first = steps_for(store_conn, "r1")
    assert [s["step"] for s in first] == ["task/t1/build", "task/t1/plan", "task/t1/review"]
    save_task_result(c, LIFECYCLE, phase="p1", task="t1")
    assert steps_for(store_conn, "r1") == first
    save_task_result(c, CHECKED, phase="p1", task="t1")
    save_task_result(c, CHECKED, phase="p1", task="t1")
    steps = steps_for(store_conn, "r1")
    assert [s["kind"] for s in steps] == ["build", "checks", "plan", "review"]
    assert {(s["initiative"], s["phase"], s["task_id"]) for s in steps} == {("i", "p1", "t1")}


def test_a_result_with_no_steps_writes_the_record_and_no_step(tmp_path, store_conn):
    save_task_result(ctx(tmp_path, Store(store_conn)), RESULT, phase="p1", task="t1")
    assert read.task_record(store_conn, "r1", "p1", "t1") == RESULT
    assert steps_for(store_conn, "r1") == []


PASSING = [{"name": "note", "passed": True, "exit_code": 0}]
FAILING = [{"name": "note", "passed": False, "exit_code": 1}]


def performed(conn, run_id):
    return [(s["step"], s["ok"], s["verdict"], s["result_json"].get("checks")) for s in steps_for(conn, run_id)]


def test_a_resumed_run_writes_only_the_checks_it_ran_on_a_reused_result(tmp_path, store_conn):
    # r1 built, reviewed and checked t1 (its checks failed); r2 resumes from r1, reuses the approved result as
    # `epic_phase` does (`without_cause(saved)`), saves it, then runs the checks again and saves them.
    reused = with_checks(LIFECYCLE, FAILING, "r1")
    c = replace(ctx(tmp_path, Store(store_conn)), run_id="r2")
    save_task_result(c, reused, phase="p1", task="t1")
    assert performed(store_conn, "r2") == []
    save_task_result(c, with_checks(reused, PASSING, "r2"), phase="p1", task="t1")
    assert performed(store_conn, "r2") == [("task/t1/checks", 1, "", [{**PASSING[0], "outcome": ""}])]


def test_a_rescue_writes_its_own_review_and_never_the_quarantined_attempts(tmp_path, store_conn):
    # `epic_execute.rescue_task`: the quarantined attempt's result (r1, review revise) is saved under the rescue run
    # with the rescue's check rows, then the rescue review's result (the kept patch, not built) is saved.
    quarantined = {**LIFECYCLE, "review": {"verdict": "revise", "findings": ["f"]}, "evidence": [{"check": "c"}]}
    c = replace(ctx(tmp_path, Store(store_conn)), run_id="rescue-1")
    save_task_result(c, quarantined, phase="p1", task="t1")
    assert performed(store_conn, "rescue-1") == []
    rescue = {"run_id": "rescue-1:p1:t1", "plan": None, "build": {"patch": "--- a\n+++ b\n", "reused": True},
              "review": {"verdict": "approve", "findings": []}, "adversary": None, "arbitration": None,
              "fix_loop": {"attempts": 1}}
    save_task_result(c, rescue, phase="p1", task="t1")
    assert performed(store_conn, "rescue-1") == [("task/t1/review", 1, "approve", None)]


def test_a_refix_keeps_the_checks_that_sent_the_attempt_back_and_the_re_entrys_own(tmp_path, store_conn):
    # `epic_build.refix_task`: the first attempt's checks fail, they are recorded under it, a re-entry runs as
    # `<run>:<phase>:refix1`, and its passing checks are saved with it (`epic_phase`).
    c = ctx(tmp_path, Store(store_conn))
    save_task_result(c, LIFECYCLE, phase="p1", task="t1")
    record_steps(c, with_checks(LIFECYCLE, FAILING, "r1"), phase="p1", task="t1")
    retried = {**LIFECYCLE, "run_id": "r1:p1:refix1:t1", "plan": {}, "review": {"verdict": "approve", "findings": []}}
    save_task_result(c, retried, phase="p1", task="t1")
    save_task_result(c, with_checks(retried, PASSING, "r1"), phase="p1", task="t1")
    assert performed(store_conn, "r1") == [
        ("task/t1-refix-1/build", 1, "", None),
        ("task/t1-refix-1/checks", 1, "", [{**PASSING[0], "outcome": ""}]),
        ("task/t1-refix-1/review", 1, "approve", None),
        ("task/t1/build", 1, "", None),
        ("task/t1/checks", 0, "", [{**FAILING[0], "outcome": ""}]),
        ("task/t1/plan", 1, "", None),
        ("task/t1/review", 1, "approve", None),
    ]


def test_a_refix_re_entry_that_is_not_approved_still_writes_the_steps_it_ran(tmp_path, store_conn):
    # `refix_task` breaks on an unapproved re-entry without saving it; `record_steps` writes what it ran.
    c = ctx(tmp_path, Store(store_conn))
    refused = {**LIFECYCLE, "run_id": "r1:p1:refix1:t1", "plan": {}, "review": {"verdict": "revise", "findings": ["f"]}}
    record_steps(c, refused, phase="p1", task="t1")
    assert performed(store_conn, "r1") == [("task/t1-refix-1/build", 1, "", None), ("task/t1-refix-1/review", 0, "revise", None)]


def test_a_rescue_whose_review_revises_writes_that_review(tmp_path, store_conn):
    # `rescue_task` records the rescue graph's steps before it returns `rescue_failed` on a revise.
    c = replace(ctx(tmp_path, Store(store_conn)), run_id="rescue-1")
    revised = {"run_id": "rescue-1:p1:t1", "build": {"patch": "x", "reused": True},
               "review": {"verdict": "revise", "findings": ["f", "g"]}, "fix_loop": {"attempts": 1}}
    record_steps(c, revised, phase="p1", task="t1")
    assert performed(store_conn, "rescue-1") == [("task/t1/review", 0, "revise", None)]


def test_the_checks_before_a_style_pass_keep_a_row_of_their_own(tmp_path, store_conn):
    # `refix_task`: the checks fail, a style-pass edit fixes them, the re-run checks are saved as the attempt's.
    c = ctx(tmp_path, Store(store_conn))
    record_steps(c, with_checks(LIFECYCLE, FAILING, "r1", stage="prestyle"), phase="p1", task="t1")
    save_task_result(c, with_checks(LIFECYCLE, PASSING, "r1"), phase="p1", task="t1")
    checks = [p for p in performed(store_conn, "r1") if "/checks" in p[0]]
    assert checks == [
        ("task/t1/checks", 1, "", [{**PASSING[0], "outcome": ""}]),
        ("task/t1/checks-prestyle", 0, "", [{**FAILING[0], "outcome": ""}]),
    ]
