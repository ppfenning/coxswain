from datetime import UTC, datetime

from agent_tools.chair_report import format_status, unlaunched_lines

NOW = datetime(2026, 11, 4, 18, 5, tzinfo=UTC)
STAMP = "chair 11-04 13:05 EST"
FACTS = {
    "limits": {"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 2,
               "go_degraded": False, "five_hour_fraction": 0.4, "window_start_day": None, "window_source": "meter",
               "weekly_source": "meter"},
    "dispatch": {"max_in_flight": 4, "live_runs": 0},
}
A = {"kind": "unlaunched", "initiative": "epic-a", "reason": "no free lane", "epoch": 0}
B = {"kind": "unlaunched", "initiative": "epic-b", "reason": "shares paths with epic-a", "epoch": 0}


def test_two_actions_give_two_lines_in_order():
    assert unlaunched_lines([A, B], NOW) == [
        f"{STAMP} not launched epic-a: no free lane",
        f"{STAMP} not launched epic-b: shares paths with epic-a",
    ]


def test_no_action_gives_no_line():
    assert unlaunched_lines([], NOW) == []


def test_other_kinds_are_ignored():
    other = {"kind": "launch_epic", "initiative": "epic-c", "epoch": 0}
    assert unlaunched_lines([other, B], NOW) == [f"{STAMP} not launched epic-b: shares paths with epic-a"]


def test_stamp_matches_the_status_line_stamp():
    status = format_status(FACTS, [], [], NOW)
    assert unlaunched_lines([A], NOW)[0].startswith(status.split(" | ")[0] + " not launched")
