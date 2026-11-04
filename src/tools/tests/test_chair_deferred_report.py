from datetime import UTC, datetime

from agent_tools.chair_exec import Deps, argv_for, perform
from agent_tools.chair_report import deferred_items, failed_items, format_status, lands_this_tick

NOW = datetime(2026, 11, 4, 18, 5, tzinfo=UTC)
DEFERRED = {"kind": "deferred", "initiative": "epic-a", "phase": "p1", "land_at": "ticket", "repo": "r", "epoch": 0}
LINE = "deferred: epic-a p1 (deferred to the last phase, land_at ticket)"
FACTS = {
    "limits": {"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 2,
               "go_degraded": False, "five_hour_fraction": 0.4, "window_start_day": None, "window_source": "meter",
               "weekly_source": "meter"},
    "dispatch": {"max_in_flight": 4, "live_runs": 0},
}


def _run(action, epoch=0):
    ran, recorded = [], []
    deps = Deps(
        run=lambda argv: ran.append(argv) or (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda a: "run-1",
        repo_for=lambda a: "r",
    )
    return perform([action], deps, lambda: epoch, False), ran, recorded


def test_deferred_has_no_argv():
    assert argv_for(DEFERRED) is None


def test_deferred_is_recorded_without_running_a_command():
    results, ran, recorded = _run(DEFERRED)
    assert [r["status"] for r in results] == ["recorded"]
    assert results[0]["reason"] == "deferred to the last phase (land_at ticket)"
    assert ran == []
    assert [r["status"] for r in recorded] == ["recorded"]


def test_deferred_is_never_epoch_fenced_or_escalated():
    results, ran, recorded = _run(DEFERRED, epoch=-1)
    assert len(results) == 1 and results[0]["status"] == "recorded"
    assert ran == [] and len(recorded) == 1


def test_deferred_report_line_names_the_phase_and_the_land_at():
    results, _, _ = _run(DEFERRED)
    assert deferred_items(results) == [LINE]
    assert LINE in format_status(FACTS, [], results, NOW).split(" | ")


def test_deferred_counts_as_neither_landed_nor_failed():
    results, _, _ = _run(DEFERRED)
    assert lands_this_tick(results) == 0
    assert failed_items(results) == []
    line = format_status(FACTS, [], results, NOW)
    assert "lands 0" in line
    assert "failed:" not in line
