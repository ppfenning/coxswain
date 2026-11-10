from datetime import UTC, datetime

from agent_tools.chair_exec import Deps, perform
from agent_tools.chair_report import format_status, lands_this_tick

NOW = datetime(2026, 11, 4, 18, 5, tzinfo=UTC)
LAND = {"kind": "land_phase", "initiative": "epic-a", "phase": "p1", "run": "run-1", "repo": "r", "epoch": 0}
FACTS = {
    "limits": {"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 2,
               "go_degraded": False, "five_hour_fraction": 0.4, "window_start_day": None, "window_source": "meter",
               "weekly_source": "meter"},
    "dispatch": {"max_in_flight": 4, "live_runs": 0},
}


def _land_first_ticket_phase():
    deps = Deps(
        run=lambda argv: (0, "merge: ok\nmark_done: ok\n"),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda a: "run-1",
        repo_for=lambda a: "r",
        rev_parse=lambda repo, ref: None,
    )
    return perform([LAND], deps, lambda: 0, False)


def test_a_first_ticket_phase_land_counts_as_one_land() -> None:
    assert lands_this_tick(_land_first_ticket_phase()) == 1


def test_a_first_ticket_phase_land_prints_no_deferred_text() -> None:
    parts = format_status(FACTS, [], _land_first_ticket_phase(), NOW).split(" | ")
    assert "lands 1" in parts
    assert not [p for p in parts if "deferred" in p]
    assert not [p for p in parts if p.startswith("deferred:")]
