from agent_tools.chair_exec import Deps, argv_for, perform
from agent_tools.chair_run import changed_row_ids

UNLAUNCHED = {"kind": "unlaunched", "initiative": "x", "reason": "waiting on a free lane", "epoch": 0}


def _run(action, epoch, dry_run=False):
    ran, recorded = [], []
    deps = Deps(
        run=lambda argv: ran.append(argv) or (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda a: "run-1",
        repo_for=lambda a: "r",
    )
    return perform([action], deps, lambda: epoch, dry_run), ran, recorded


def test_unlaunched_has_no_argv():
    assert argv_for(UNLAUNCHED) is None


def test_live_returns_a_non_failing_result_and_runs_nothing():
    results, ran, recorded = _run(UNLAUNCHED, 0)
    assert (results[0]["status"], results[0]["reason"]) == ("skipped", "waiting on a free lane")
    assert ran == []
    assert recorded == []


def test_dry_run_returns_the_live_result():
    live, _, _ = _run(UNLAUNCHED, 0)
    dry, ran, recorded = _run(UNLAUNCHED, 0, dry_run=True)
    assert dry == live
    assert dry[0]["status"] != "dry_run"
    assert ran == []
    assert recorded == []


def test_stale_epoch_is_not_refused():
    for dry_run in (False, True):
        results, ran, _ = _run(UNLAUNCHED, -1, dry_run)
        assert results[0]["status"] == "skipped"
        assert ran == []


def test_missing_epoch_is_not_refused():
    action = {k: v for k, v in UNLAUNCHED.items() if k != "epoch"}
    results, ran, _ = _run(action, 5)
    assert results[0]["status"] == "skipped"
    assert ran == []


def test_changed_rows_check_does_not_count_it():
    results, _, _ = _run({**UNLAUNCHED, "task_id": "t1"}, 0)
    assert changed_row_ids(results) == ()
