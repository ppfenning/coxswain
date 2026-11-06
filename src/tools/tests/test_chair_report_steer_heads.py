from datetime import UTC, datetime

from agent_tools.chair_report import format_status, steer_head_items

NOW = datetime(2026, 11, 6, 18, 5, tzinfo=UTC)
FACTS = {
    "limits": {"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 2,
               "go_degraded": False, "five_hour_fraction": 0.4, "window_start_day": None,
               "window_source": "meter", "weekly_source": "meter"},
    "dispatch": {"max_in_flight": 4, "live_runs": 2},
}


def _result(kind, status, **fields):
    return {"action": {"kind": kind, **fields}, "status": status, "reason": ""}


def _steer(initiative, other):
    return _result("steer_clear", "recorded", initiative=initiative, other=other, paths=["src/main.rs"])


CHAIN = [_steer("C", "B"), _steer("B", "A")]


def test_idle_head_of_a_two_link_chain():
    assert steer_head_items(CHAIN) == ["A (2 waiting, idle)"]
    line = format_status(FACTS, [], CHAIN, NOW)
    assert "steered: C~B, B~A | steer heads: A (2 waiting, idle)" in line


def test_launch_result_for_the_head_flips_it_to_launched():
    results = [*CHAIN, _result("launch_epic", "done", initiative="A")]
    assert steer_head_items(results) == ["A (2 waiting, launched)"]


def test_refused_launch_leaves_the_head_idle():
    results = [*CHAIN, _result("launch_epic", "refused", initiative="A")]
    assert steer_head_items(results) == ["A (2 waiting, idle)"]


def test_no_steer_result_adds_no_part():
    results = [_result("launch_epic", "done", initiative="A")]
    assert steer_head_items(results) == []
    assert "steer heads" not in format_status(FACTS, [], results, NOW)


def test_self_steer_does_not_hide_the_head():
    results = [_steer("Y", "X"), _steer("X", "X")]
    assert steer_head_items(results) == ["X (1 waiting, idle)"]
    assert "steer heads: X (1 waiting, idle)" in format_status(FACTS, [], results, NOW)


def test_self_steer_alone_adds_no_part():
    assert steer_head_items([_steer("X", "X")]) == []


def test_one_initiative_steered_behind_two_others_counts_for_each():
    results = [_steer("Y", "X"), _steer("Y", "Z")]
    assert steer_head_items(results) == ["X (1 waiting, idle)", "Z (1 waiting, idle)"]


def test_cycle_has_no_head_and_terminates():
    assert steer_head_items([_steer("A", "B"), _steer("B", "A")]) == []


def test_result_missing_a_name_is_skipped():
    results = [_result("steer_clear", "recorded", initiative="Y"), _result("steer_clear", "recorded", other="X")]
    assert steer_head_items(results) == []
