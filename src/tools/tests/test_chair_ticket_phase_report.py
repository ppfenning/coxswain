from datetime import UTC, datetime

from agent_tools import chair_exec
from agent_tools.chair_exec import Deps, perform
from agent_tools.chair_report import failed_items, format_status, lands_this_tick

NOW = datetime(2026, 11, 4, 18, 5, tzinfo=UTC)
LAND_PHASE = {"kind": "land_phase", "initiative": "epic-a", "phase": "p1", "run": "run-1", "repo": "r", "epoch": 0}
LANDED = "merge: ok\nmark_done: ok\n"
FACTS = {
    "limits": {"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 2,
               "go_degraded": False, "five_hour_fraction": 0.4, "window_start_day": None, "window_source": "meter",
               "weekly_source": "meter"},
    "dispatch": {"max_in_flight": 4, "live_runs": 0},
}


def _run(action, monkeypatch):
    monkeypatch.setattr(chair_exec, "land_commit", lambda repo: "abc123")
    deps = Deps(
        run=lambda argv: (0, LANDED),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda result: None,
        run_id=lambda a: "run-1",
        repo_for=lambda a: "r",
        rev_parse=lambda repo, ref: None,
    )
    return perform([action], deps, lambda: 0, False)


def test_a_first_ticket_phase_land_counts_as_one_land_and_reports_nothing_deferred(monkeypatch):
    results = _run(LAND_PHASE, monkeypatch)
    line = format_status(FACTS, [], results, NOW)
    assert [r["status"] for r in results] == ["landed"]
    assert lands_this_tick(results) == 1 and failed_items(results) == []
    assert "lands 1" in line
    assert "deferred" not in line
    assert not any(part.startswith("deferred:") for part in line.split(" | "))
