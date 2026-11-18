"""H7.1: a stall alarm for the landing model. One courier and notify event when a stall starts, one all-clear when it ends."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_tools import chair_exec, chair_facts, chair_read_record, cli, courier, run_store, store_cli
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_stall_alarm import (
    DEFAULT_LAND_HOURS,
    DEFAULT_LAUNCH_MINUTES,
    DEFAULT_REPEAT_TICKS,
    attempt_heads,
    failure_streaks,
    land_facts,
    last_launch_at,
    normal_reason,
    observe,
    open_alarms,
    plan_alarms,
    plan_stall_alarm,
    repeat_ending,
    repeat_key,
    resolve_settings,
    stall_facts,
)
from agent_tools.notify_events import events_from_tick
from harness import landing_store
from harness.store_migrate import open_store

_NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
_SETTINGS = {"land_hours": 6.0, "launch_minutes": 60.0, "repeat_ticks": 3}


def _ts(hours_ago: float = 0.0, now: datetime = _NOW) -> str:
    return (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(initiative, state, created, phases=None, updated=None):
    return {"initiative": initiative, "state": state, "created_at": created, "updated_at": updated or created,
            "phases": phases or {}}


def _row(kind, status, hours_ago, reason="", target="demo-t1", **extra):
    return {"kind": kind, "status": status, "ts": _ts(hours_ago), "reason": reason, "target": target, **extra}


# --- settings ---

_DEFAULTS = {"land_hours": DEFAULT_LAND_HOURS, "launch_minutes": DEFAULT_LAUNCH_MINUTES, "repeat_ticks": DEFAULT_REPEAT_TICKS}


@pytest.mark.parametrize("value, expected", [
    (None, _DEFAULTS),
    ({"land_hours": 2, "launch_minutes": 0, "repeat_ticks": 5}, {"land_hours": 2.0, "launch_minutes": 0.0, "repeat_ticks": 5}),
    ({"land_hours": -1, "launch_minutes": "x", "repeat_ticks": 1}, _DEFAULTS),
    ({"land_hours": True, "repeat_ticks": 0}, {**_DEFAULTS, "repeat_ticks": 0}),
    ({"repeat_ticks": 2.5}, _DEFAULTS),
    ("not a mapping", _DEFAULTS),
])
def test_settings_take_numbers_of_at_least_zero_and_fall_back_to_the_defaults(value, expected):
    assert resolve_settings(value) == expected


# --- the land facts ---

def test_the_newest_land_is_a_merged_phase_or_a_landed_record_and_open_is_neither_landed_nor_draft():
    records = [
        _record("alpha", "assembled", _ts(10), phases={"p1": {"state": "merged", "updated_at": _ts(8)}, "p2": {"state": "open", "updated_at": _ts(1)}}),
        _record("beta", "landed", _ts(20), updated=_ts(7)),
        _record("gamma", "draft", _ts(1)),
        _record("delta", "building", _ts(9)),
    ]
    assert land_facts(records) == (_ts(7), {"alpha": _ts(10), "delta": _ts(9)})
    assert land_facts([]) == (None, {})


# --- the action rows ---

def test_the_last_launch_is_the_newest_done_launch_row():
    rows = [_row("launch_epic", "done", 5), _row("relaunch", "done", 2), _row("relaunch", "failed", 1), _row("land", "done", 0)]
    assert last_launch_at(rows) == _ts(2)
    assert last_launch_at([_row("land", "done", 0)]) is None


def test_a_reason_that_differs_only_in_its_numbers_is_the_same_reason():
    assert normal_reason("gh  exited 1 after\n 30s on pr 1204") == "gh exited # after #s on pr #"


def test_three_failures_in_a_row_with_one_reason_make_a_streak_that_names_the_newest_raw_reason():
    rows = [
        _row("land", "landed", 5),
        _row("land", "refused", 3, "checks failed on pr 12"),
        _row("land", "refused", 2, "checks failed on pr 13"),
        _row("needs_chair", "done", 2, "a report between attempts is not an attempt"),
        _row("land", "refused", 1, "checks  failed on pr 14"),
    ]
    assert failure_streaks(rows, 3) == [{
        "kind": "land", "target": "demo-t1", "initiative": "demo-t1", "reason": "checks failed on pr #",
        "raw": "checks failed on pr 14", "count": 3, "since": _ts(3), "newest": _ts(1),
    }]
    assert failure_streaks(rows, 4) == []
    assert failure_streaks(rows, 0) == []


@pytest.mark.parametrize("newest", [
    _row("land", "landed", 0.5),
    _row("land", "refused", 0.5, "another reason"),
])
def test_a_success_or_another_reason_ends_the_streak(newest):
    rows = [_row("land", "refused", h, "checks failed") for h in (3, 2, 1)]
    assert failure_streaks([*rows, newest], 3) == []


def test_each_action_and_target_has_its_own_streak_and_its_own_head():
    rows = [_row("land", "refused", h, "red 1", target=t) for h in (3, 2, 1) for t in ("a", "b")]
    rows.append(_row("land", "landed", 0.5, target="b"))
    assert [s["target"] for s in failure_streaks(rows, 3)] == ["a"]
    assert attempt_heads(rows) == {
        "repeat:land:a": {"failed": True, "reason": "red #", "ts": _ts(1)},
        "repeat:land:b": {"failed": False, "reason": "", "ts": _ts(0.5)},
    }


def test_an_alarm_is_open_from_its_raise_until_its_clear_and_only_done_rows_count():
    raise_row = _row("stall_alarm", "done", 3, stall="no_land", since=_ts(9), detail="no land", subject="alpha")
    assert open_alarms([raise_row]) == {"no_land": {"since": _ts(9), "detail": "no land", "subject": "alpha"}}
    assert open_alarms([raise_row, _row("stall_clear", "done", 1, stall="no_land")]) == {}
    assert open_alarms([raise_row, _row("stall_clear", "failed", 1, stall="no_land")]) != {}
    assert open_alarms([_row("stall_alarm", "failed", 3, stall="no_land")]) == {}


# --- the checks ---

def _facts(**over):
    base = {"settings": dict(_SETTINGS), "last_land_at": None, "open_initiatives": {}, "last_launch_at": None,
            "watch_since": _ts(48), "heads": {}, "streaks": [], "open": {}}
    return {**base, **over}


def _gate(**over):
    base = {"free_lanes": 0, "ready": 0, "subject": "", "held": frozenset(), "idle_raised": False, "paused": False}
    return {**base, **over}


def test_no_land_for_the_set_hours_while_initiatives_are_open_is_a_stall():
    late = _facts(last_land_at=_ts(7), open_initiatives={"alpha": _ts(30), "beta": _ts(20)})
    (stall,) = observe(late, _gate(), _NOW)
    assert (stall["key"], stall["since"], stall["subject"]) == ("no_land", _ts(7), "alpha")
    assert stall["detail"].startswith("no land for 7.0h while 2 initiative(s) are open (alpha, beta)")


@pytest.mark.parametrize("facts, gate", [
    (_facts(last_land_at=_ts(5), open_initiatives={"alpha": _ts(30)}), _gate()),
    (_facts(last_land_at=_ts(30), open_initiatives={"alpha": _ts(2)}), _gate()),
    (_facts(last_land_at=_ts(30), open_initiatives={"alpha": _ts(30)}, watch_since=_ts(2)), _gate()),
    (_facts(watch_since=None, open_initiatives={"alpha": _ts(30)}), _gate()),
    (_facts(last_land_at=_ts(30), open_initiatives={}), _gate()),
    (_facts(last_land_at=_ts(30), open_initiatives={"alpha": _ts(30)}), _gate(held=frozenset({"alpha"}))),
    (_facts(last_land_at=_ts(30), open_initiatives={"alpha": _ts(30)}), _gate(paused=True)),
    (_facts(last_land_at=_ts(30), open_initiatives={"alpha": _ts(30)}, settings={**_SETTINGS, "land_hours": 0}), _gate()),
], ids=["recent-land", "new-initiative", "new-chair", "no-chair-row", "nothing-open", "held", "hard-stop", "check-off"])
def test_no_land_is_not_a_stall_when(facts, gate):
    assert observe(facts, gate, _NOW) == []


def test_a_first_initiative_with_no_land_yet_is_late_from_its_creation():
    (stall,) = observe(_facts(open_initiatives={"alpha": _ts(6)}), _gate(), _NOW)
    assert stall["since"] == _ts(6) and stall["detail"].endswith("nothing has landed yet")


def test_free_lanes_and_ready_work_with_no_launch_for_the_set_minutes_is_a_stall():
    facts = _facts(last_launch_at=_ts(2))
    idle = _gate(free_lanes=2, ready=3, subject="alpha")
    (stall,) = observe(facts, idle, _NOW)
    assert (stall["key"], stall["since"], stall["subject"]) == ("no_launch", _ts(2), "alpha")
    assert stall["detail"].endswith(f"the last launch was at {_ts(2)}")
    assert observe(facts, {**idle, "free_lanes": 0}, _NOW) == []
    assert observe(facts, {**idle, "ready": 0}, _NOW) == []
    assert observe(facts, {**idle, "idle_raised": True}, _NOW) == []  # the chair's idle line says it already
    assert observe(facts, {**idle, "paused": True}, _NOW) == []
    assert observe(_facts(last_launch_at=_ts(0.5)), idle, _NOW) == []


def test_with_no_launch_ever_the_no_launch_clock_starts_at_the_chair_s_first_row():
    idle = _gate(free_lanes=1, ready=1, subject="alpha")
    (stall,) = observe(_facts(watch_since=_ts(2)), idle, _NOW)
    assert stall["since"] == _ts(2) and stall["detail"].endswith("nothing has launched yet")
    assert observe(_facts(watch_since=_ts(0.5)), idle, _NOW) == []
    assert observe(_facts(watch_since=None), idle, _NOW) == []


_STREAK = {"kind": "land", "target": "demo-t1", "initiative": "demo", "reason": "red #", "raw": "red 7", "count": 3,
           "since": _ts(1), "newest": _ts(0.5)}


def test_a_live_streak_of_the_set_length_is_a_stall_naming_its_newest_reason_and_a_stale_one_is_not():
    (stall,) = observe(_facts(streaks=[_STREAK]), _gate(paused=True), _NOW)
    assert (stall["key"], stall["subject"]) == (repeat_key(_STREAK), "demo")
    assert stall["key"].startswith("repeat:land:demo-t1:")
    assert stall["detail"] == "land demo-t1 failed 3 times in a row; the newest: red 7"
    assert observe(_facts(streaks=[{**_STREAK, "newest": _ts(2)}]), _gate(), _NOW) == []
    assert observe(_facts(streaks=[_STREAK], settings={**_SETTINGS, "repeat_ticks": 4}), _gate(), _NOW) == []


# --- raise once, clear once ---

_STALL = {"key": "no_land", "since": _ts(7), "subject": "alpha", "detail": "no land for 7.0h"}
_OPEN = {"no_land": {"since": _ts(7), "detail": "no land for 7.0h", "subject": "alpha"}}


def test_a_standing_stall_is_raised_once_and_cleared_once():
    assert plan_alarms([_STALL], _facts(), _NOW) == [
        {"kind": "stall_alarm", "stall": "no_land", "since": _ts(7), "subject": "alpha", "detail": "no land for 7.0h"}
    ]
    assert plan_alarms([_STALL], _facts(open=_OPEN), _NOW) == []
    assert plan_alarms([], _facts(open=_OPEN), _NOW) == [
        {"kind": "stall_clear", "stall": "no_land", "since": _ts(7), "subject": "alpha", "detail": "cleared: no land for 7.0h"}
    ]
    assert plan_alarms([], _facts(open=_OPEN), _NOW, paused=True) == []  # the hard stop judges no land at all
    assert plan_alarms([], _facts(), _NOW) == []


_KEY = repeat_key(_STREAK)
_DIGEST_HEAD = {"failed": True, "reason": "red #", "ts": _ts(0.5)}


@pytest.mark.parametrize("head, ending", [
    (_DIGEST_HEAD, "standing"),
    ({**_DIGEST_HEAD, "ts": _ts(2)}, "quiet"),
    (None, "quiet"),
    ({"failed": False, "reason": "", "ts": _ts(0.5)}, "broken"),
    ({**_DIGEST_HEAD, "reason": "another"}, "broken"),
], ids=["same-and-recent", "went-quiet", "rows-gone", "succeeded", "failed-otherwise"])
def test_a_raised_same_failure_stall_stands_breaks_or_goes_quiet(head, ending):
    heads = {} if head is None else {"repeat:land:demo-t1": head}
    assert repeat_ending(_KEY, heads, _NOW) == ending


def test_a_broken_streak_is_cleared_aloud_and_a_quiet_one_expires_unsaid():
    opened = {_KEY: {"since": _ts(1), "detail": "land demo-t1 failed", "subject": "demo"}}
    broken = _facts(open=opened, heads={"repeat:land:demo-t1": {"failed": False, "reason": "", "ts": _ts(0.1)}})
    assert plan_alarms([], broken, _NOW) == [
        {"kind": "stall_clear", "stall": _KEY, "since": _ts(1), "subject": "demo", "detail": "cleared: land demo-t1 failed"}
    ]
    quiet = _facts(open=opened, heads={"repeat:land:demo-t1": {**_DIGEST_HEAD, "ts": _ts(3)}})
    assert plan_alarms([], quiet, _NOW) == [
        {"kind": "stall_clear", "stall": _KEY, "since": _ts(1), "subject": "demo", "detail": "expired: land demo-t1 failed",
         "quiet": True}
    ]
    standing = _facts(open=opened, heads={"repeat:land:demo-t1": _DIGEST_HEAD})
    assert plan_alarms([], standing, _NOW) == []  # the count dropped below the setting, but the streak is not broken


def _plan_facts(**over):
    alarm = _facts(last_launch_at=_ts(2), last_land_at=_ts(8), open_initiatives={"alpha": _ts(8)})
    return {"stall_alarm": alarm, "idle_stall": {"free_lanes": 2, "ready": 1, "queued": 0, "open_signature": None},
            "initiatives": [{"id": "alpha", "ready_tasks": [{"id": "t"}]}], **over}


def test_plan_reads_the_hard_stop_the_idle_line_and_the_held_initiatives():
    assert [a["stall"] for a in plan_stall_alarm(_plan_facts(), [], _NOW)] == ["no_land", "no_launch"]
    assert plan_stall_alarm(_plan_facts(), [], _NOW, launch_paused=True) == []
    # a launch planned this tick is not a launch: only one that completed counts
    assert [a["stall"] for a in plan_stall_alarm(_plan_facts(), [{"kind": "launch_epic"}], _NOW)] == ["no_land", "no_launch"]
    idle_line = [{"kind": "needs_chair", "cause": "idle_stall"}]
    assert [a["stall"] for a in plan_stall_alarm(_plan_facts(), idle_line, _NOW)] == ["no_land"]
    open_idle = _plan_facts(idle_stall={"free_lanes": 2, "ready": 1, "queued": 0, "open_signature": "host:h"})
    assert [a["stall"] for a in plan_stall_alarm(open_idle, [], _NOW)] == ["no_land"]
    assert [a["stall"] for a in plan_stall_alarm(_plan_facts(held_initiatives=["alpha"]), [], _NOW)] == ["no_launch"]
    assert [a["stall"] for a in plan_stall_alarm(_plan_facts(no_progress={"alpha": {}}), [], _NOW)] == ["no_launch"]
    assert plan_stall_alarm({}, [], _NOW) == []


def test_a_raise_and_its_clear_push_one_event_each_and_a_quiet_expiry_pushes_none():
    raise_action = {"kind": "stall_alarm", "stall": "no_land", "since": _ts(7), "subject": "alpha", "detail": "no land for 7.0h"}
    clear_action = {**raise_action, "kind": "stall_clear", "detail": "cleared: no land for 7.0h"}
    (raised,) = events_from_tick({}, [raise_action, raise_action])
    (cleared,) = events_from_tick({}, [clear_action])
    assert (raised.kind, raised.key, raised.title) == ("stall", f"stall_alarm:no_land:{_ts(7)}", "Stalled: no_land")
    assert (cleared.kind, cleared.key, cleared.title) == ("all_clear", f"stall_clear:no_land:{_ts(7)}", "All clear: no_land")
    assert events_from_tick({}, [{**clear_action, "quiet": True}]) == []


def test_stall_facts_gathers_every_check_s_input():
    docs = [_row("relaunch", "done", 3), *(_row("land", "refused", h, "red") for h in (2.5, 2, 1.5))]
    facts = stall_facts(resolve_settings(None), [_record("alpha", "building", _ts(9))], docs)
    assert (facts["last_land_at"], facts["open_initiatives"], facts["watch_since"]) == (None, {"alpha": _ts(9)}, _ts(3))
    assert facts["last_launch_at"] == _ts(3)
    assert [s["count"] for s in facts["streaks"]] == [3]
    assert list(facts["heads"]) == ["repeat:land:demo-t1", "repeat:relaunch:demo-t1"]
    assert facts["open"] == {}


# --- end to end: the chair's real gather, plan and executor over a throwaway store ---

@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace with a throwaway store whose harness python is this interpreter, so the chair's own store commands
    (the action recorder among them) run for real against it."""
    python = tmp_path / "harness" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\nharness_dir: {tmp_path / 'harness'}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    (tmp_path / "runs").mkdir()
    open_store(run_store.store_url_for(tmp_path / "runs"), "2026-01-01T00:00:00Z").close()
    return tmp_path


_PROFILE_ALARM = {"land_hours": 2, "launch_minutes": 0, "repeat_ticks": 3}


def _deps(ws: Path):
    profile = {"workspace_dir": str(ws), "harness_dir": str(ws / "harness"), "chair": {"stall_alarm": _PROFILE_ALARM}}
    return cli._chair_run_deps(ws / "runs", profile, "chair", 1, "h", False, lambda line: None, ws / "profile.yaml", "store")


def _put(ws: Path, record: dict) -> None:
    conn = open_store(run_store.store_url_for(ws / "runs"), "2026-01-01T00:00:00Z")
    try:
        landing_store.put_record(conn, record)
    finally:
        conn.close()


def _recorder(ws: Path, epoch: int, seconds_ago: list[int] | None = None):
    """The chair's own recorder. A row's key holds its second, so `seconds_ago` stamps rows written within one second
    as the separate ticks they stand for, one entry per row; without it the clock is the real one."""
    def clock() -> str:
        back = seconds_ago.pop(0) if seconds_ago else 0
        return (datetime.now(UTC) - timedelta(seconds=back)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return chair_read_record.recorder(ws / "runs", lambda: epoch, clock, store=store_cli.runner(ws / "runs"), holder="chair")


def _tick(ws: Path, later: timedelta = timedelta(0)) -> list[dict]:
    """One tick as the chair runs it: the real gather over the chair's wiring, the real plan, and the real executor
    over its edge bundle, every result recorded in the store by the real recorder. Returns this tick's alarm actions.
    `later` runs the tick's clock ahead, as a tick that long from now would see it."""
    now = datetime.now(UTC) + later
    facts = chair_facts.gather_facts(_deps(ws).facts_deps, now)
    planned = plan_tick(facts, now)
    epoch = facts["lease"]["epoch"]
    deps = chair_exec.edge_deps(ws / "runs", ws, "chair", 1, run_id=lambda a: "", repo_for=lambda a: "",
                                record=_recorder(ws, epoch), host="h", harness_python=sys.executable)
    chosen = [a for a in planned if a["kind"] in ("take_lease", "stall_alarm", "stall_clear")]
    results = chair_exec.perform(chosen, deps, lambda: epoch, False)
    assert all(r["status"] == "done" for r in results), results
    return [a for a in chosen if a["kind"] != "take_lease"]


def _courier(ws: Path) -> list[dict]:
    path = ws / "courier.jsonl"
    return courier.entries(path.read_text(encoding="utf-8")) if path.is_file() else []


def test_end_to_end_no_land_raises_one_event_and_one_all_clear_when_a_phase_merges(ws):
    now = datetime.now(UTC)
    _put(ws, {"initiative": "demo", "repo": "r", "branch": "init/demo", "parent": "main", "state": "building",
              "phases": {"p1": {"state": "merged", "updated_at": _ts(3, now)}}, "created_at": _ts(5, now), "updated_at": _ts(3, now)})
    assert _tick(ws) == []  # the first tick takes the lease, and the chair has only just started watching
    _recorder(ws, 0, [4 * 3600])({"kind": "housekeeping", "status": "done", "reason": ""})  # it has watched for hours
    (raised,) = _tick(ws)
    assert (raised["kind"], raised["stall"], raised["subject"]) == ("stall_alarm", "no_land", "demo")
    assert [e["event"] for e in _courier(ws)] == ["stall_raised"]
    assert _tick(ws) == [] and _tick(ws) == []  # raised once: the recorded row marks it open
    assert len(_courier(ws)) == 1

    _put(ws, {"initiative": "demo", "repo": "r", "branch": "init/demo", "parent": "main", "state": "building",
              "phases": {"p1": {"state": "merged", "updated_at": _ts(3, now)}, "p2": {"state": "merged", "updated_at": _ts(0, now)}},
              "created_at": _ts(5, now), "updated_at": _ts(0, now)})
    (cleared,) = _tick(ws)
    assert (cleared["kind"], cleared["stall"], cleared["since"]) == ("stall_clear", "no_land", raised["since"])
    entries = _courier(ws)
    assert [e["event"] for e in entries] == ["stall_raised", "stall_cleared"]
    assert entries[1]["fields"]["stall"] == "no_land" and entries[1]["ref"] == "coxswain://initiative/demo"
    assert _tick(ws) == []
    assert len(_courier(ws)) == 2


def test_end_to_end_the_same_failure_three_ticks_in_a_row_raises_once_and_a_success_clears_it(ws):
    assert _tick(ws) == []  # the lease
    record = _recorder(ws, 0, [50, 40, 30, 20, 10])
    for n in (1, 2):
        record({"kind": "land", "task_id": "demo-t1", "initiative": "demo", "status": "refused", "reason": f"checks red on pr {n}"})
    assert _tick(ws) == []  # two in a row is not yet a stall
    record({"kind": "land", "task_id": "demo-t1", "initiative": "demo", "status": "refused", "reason": "checks red on pr 3"})
    (raised,) = _tick(ws)
    assert raised["stall"].startswith("repeat:land:demo-t1:") and raised["subject"] == "demo"
    assert raised["detail"].endswith("failed 3 times in a row; the newest: checks red on pr 3")
    record({"kind": "land", "task_id": "demo-t1", "initiative": "demo", "status": "refused", "reason": "checks red on pr 4"})
    assert _tick(ws) == []  # still the same stall
    record({"kind": "land", "task_id": "demo-t1", "initiative": "demo", "status": "landed", "reason": ""})
    (cleared,) = _tick(ws)
    assert (cleared["kind"], cleared["stall"]) == ("stall_clear", raised["stall"])
    assert [e["event"] for e in _courier(ws)] == ["stall_raised", "stall_cleared"]
    rows = [json.loads(line) for line in (ws / "runs" / chair_read_record.ACTION_LOG).read_text().splitlines()]
    assert [r["kind"] for r in rows if r["kind"].startswith("stall_")] == ["stall_alarm", "stall_clear"]


def test_end_to_end_a_streak_that_goes_quiet_expires_with_no_all_clear(ws):
    assert _tick(ws) == []  # the lease
    record = _recorder(ws, 0, [30, 20, 10])
    for n in (1, 2, 3):
        record({"kind": "land", "task_id": "demo-t1", "initiative": "demo", "status": "refused", "reason": f"checks red on pr {n}"})
    (raised,) = _tick(ws)
    (expired,) = _tick(ws, later=timedelta(hours=2))  # no attempt since: the chair stopped trying, as under a hard stop
    assert (expired["kind"], expired["stall"], expired["quiet"]) == ("stall_clear", raised["stall"], True)
    assert [e["event"] for e in _courier(ws)] == ["stall_raised"]
    assert _tick(ws, later=timedelta(hours=2)) == []
