"""The stranded check of the stall alarm: a stranded approved task with no live run raises `stranded:<task>`."""

from datetime import UTC, datetime, timedelta

from agent_tools.chair_stall_alarm import observe, plan_alarms, resolve_settings

_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
_GATE = {"free_lanes": 0, "ready": 0, "subject": "", "held": frozenset(), "idle_raised": False, "paused": False,
         "busy": True, "capacity": 1, "held_items": [], "progress_at": None}  # busy: the idle check stays out of it


def _ts(minutes_ago: float) -> str:
    return (_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _facts(stranded, minutes=30, opened=None):
    return {
        "settings": {**resolve_settings(None), "stranded_minutes": minutes},
        "last_land_at": None, "open_initiatives": {}, "last_launch_at": None, "watch_since": _ts(600),
        "heads": {}, "streaks": [], "open": opened or {}, "stranded": stranded, "holds": [],
    }


def _plan(facts):
    return plan_alarms(observe(facts, _GATE, _NOW), facts, _NOW)


def test_a_stranded_task_raises_one_alarm_once_the_minutes_have_passed():
    (action,) = _plan(_facts({"t1": _ts(31)}))
    assert (action["kind"], action["stall"], action["subject"], action["since"]) == ("stall_alarm", "stranded:t1", "t1", _ts(31))


def test_a_stranded_task_is_quiet_before_the_minutes_have_passed():
    assert _plan(_facts({"t1": _ts(29)})) == []


def test_the_alarm_clears_once_the_stranded_row_is_gone():
    opened = {"stranded:t1": {"since": _ts(40), "detail": "d", "subject": "t1"}}
    (action,) = _plan(_facts({}, opened=opened))
    assert (action["kind"], action["stall"], action["subject"]) == ("stall_clear", "stranded:t1", "t1")


def test_a_raised_alarm_for_a_row_still_stranded_plans_nothing_more():
    opened = {"stranded:t1": {"since": _ts(40), "detail": "d", "subject": "t1"}}
    assert _plan(_facts({"t1": _ts(40)}, opened=opened)) == []


def test_zero_minutes_turns_the_check_off():
    assert _plan(_facts({"t1": _ts(600)}, minutes=0)) == []


def test_the_minutes_default_to_thirty_and_zero_is_kept():
    assert resolve_settings(None)["stranded_minutes"] == 30.0
    assert resolve_settings({"stranded_minutes": 0})["stranded_minutes"] == 0.0


def test_an_unknown_first_seen_time_counts_from_the_chairs_first_action():
    (action,) = _plan(_facts({"t1": ""}))
    assert (action["stall"], action["since"]) == ("stranded:t1", _ts(600))

