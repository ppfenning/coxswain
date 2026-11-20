"""The chair's idle alarm (chair_stall_alarm.idle_spell): idle with work held for too long tells the operator once.

The decision is pure: the facts, the alarms already raised (read back from the chair's own done rows) and `now` in,
the alarm actions out. A tick is re-run here by recording what the executor recorded, a done row of each action, and
planning again from it, so no test needs a mock.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_tools import chair_exec, chair_facts, chair_read_record, cli, courier, run_store, store_cli
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_stall_alarm import (
    DEFAULT_IDLE_MINUTES,
    held_items,
    initiative_holds,
    notice_word,
    open_alarms,
    plan_stall_alarm,
    profile_settings,
    resolve_settings,
    stall_message_id,
    worker_capacity,
)
from agent_tools.notify_events import events_from_tick
from agent_tools.route_profile import _parse_profile
from harness.store_migrate import open_store

_NOW = datetime(2026, 11, 20, 7, 30, tzinfo=UTC)
_TASK_RELEASE = "cox route edit {task} --initiative {initiative} --state ready"


def _ts(minutes_ago: float, now: datetime = _NOW) -> str:
    return (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _held(initiative: str, task: str, reason: str) -> dict:
    return {"initiative": initiative, "task_id": task, "phase": "p1", "reason": reason}


_SPELL_START = _ts(11 * 60)
_HELD = [_held("alpha", "t1", "needs you: platform defect A"), _held("alpha", "t2", "needs you: platform defect B"),
         _held("beta", "t3", "needs you: initiative hold")]


def _facts(*, rows=(), held=_HELD, live=0, hosts=(), max_in_flight=5, progress=None, watch=_SPELL_START, minutes=60,
           login_hosts=(), **over) -> dict:
    """One tick's facts, the alarm's part built by the same `stall_facts`-shaped mapping the gather writes."""
    alarm = {
        "settings": {**resolve_settings(None), "idle_minutes": float(minutes)},
        "last_land_at": None, "open_initiatives": {}, "last_launch_at": None, "watch_since": watch,
        "heads": {}, "streaks": [], "open": open_alarms(list(rows)), "stranded": {}, "holds": over.pop("holds", []),
    }
    return {
        "stall_alarm": alarm,
        "idle_stall": {"free_lanes": max_in_flight - live, "ready": 0, "queued": 0, "open_signature": None,
                       "last_progress_at": progress},
        "dispatch": {"max_in_flight": max_in_flight, "live_runs": live, "hosts": list(hosts)},
        "login_hosts": list(login_hosts),
        "held_tasks": list(held),
        "initiatives": [],
        **over,
    }


def _recorded(actions: list[dict], minutes_ago: float = 0) -> list[dict]:
    """The done rows the executor records for `actions`, as the next tick's facts read them back."""
    return [{**a, "status": "done", "ts": _ts(minutes_ago)} for a in actions]


# --- the decision ---

def test_every_task_held_and_no_lanes_past_the_threshold_raises_exactly_one_alarm_naming_each_item_and_its_release():
    (alarm,) = plan_stall_alarm(_facts(), [], _NOW)
    assert (alarm["kind"], alarm["stall"], alarm["since"], alarm["subject"]) == (
        "stall_alarm", f"idle_held:{_ts(11 * 60)}", _ts(11 * 60), "alpha")
    for held in _HELD:
        release = _TASK_RELEASE.format(task=held["task_id"], initiative=held["initiative"])
        assert f"- {held['task_id']} in {held['initiative']}: {held['reason']} Release: `{release}`" in alarm["detail"]
    assert [h["release"] for h in alarm["held"]] == [
        _TASK_RELEASE.format(task=h["task_id"], initiative=h["initiative"]) for h in _HELD]
    assert "3 item(s) wait on you" in alarm["detail"] and "11.0h" in alarm["detail"]


def test_the_same_spell_is_told_once_however_many_ticks_follow():
    first = plan_stall_alarm(_facts(), [], _NOW)
    rows = _recorded(first)
    for later in (1, 60, 24 * 60):
        assert plan_stall_alarm(_facts(rows=rows), [], _NOW + timedelta(minutes=later)) == []
    # a hold released or added inside the spell is not a new spell
    assert plan_stall_alarm(_facts(rows=rows, held=_HELD[:1]), [], _NOW) == []
    assert plan_stall_alarm(_facts(rows=rows, held=[]), [], _NOW) == []
    # the first recorded row moving (the action window) does not end the spell; only progress does
    assert plan_stall_alarm(_facts(rows=rows, watch=_ts(30)), [], _NOW) == []


def test_a_spell_with_work_running_produces_none():
    assert plan_stall_alarm(_facts(live=1), [], _NOW) == []
    assert plan_stall_alarm(_facts(hosts=[{"name": "lane-a", "live_runs": 1, "capacity": 2}]), [], _NOW) == []
    assert plan_stall_alarm(_facts(running=[{"initiative": "alpha"}]), [], _NOW) == []
    assert plan_stall_alarm(_facts(landing=[{"initiative": "alpha", "phase": "p1", "repo": "r"}]), [], _NOW) == []
    assert plan_stall_alarm(_facts(progress=_ts(10)), [], _NOW) == []  # it last moved ten minutes ago


def test_no_alarm_before_the_threshold_with_the_check_off_under_the_hard_stop_or_with_unknown_lanes():
    assert plan_stall_alarm(_facts(watch=_ts(59)), [], _NOW) == []
    assert plan_stall_alarm(_facts(minutes=0), [], _NOW) == []
    assert plan_stall_alarm(_facts(), [], _NOW, launch_paused=True) == []
    assert plan_stall_alarm(_facts(watch=None), [], _NOW) == []
    unknown = _facts()
    del unknown["dispatch"]
    assert plan_stall_alarm(unknown, [], _NOW) == []


def test_a_drained_everything_spell_with_nothing_held_produces_one_no_worker_capacity_note():
    drained = {"held": [], "max_in_flight": 0, "hosts": [{"name": "lane-a", "live_runs": 0, "capacity": 4}],
               "login_hosts": [{"name": "lane-a", "state": "draining"}]}
    (note,) = plan_stall_alarm(_facts(**drained), [], _NOW)
    assert note["stall"] == f"no_capacity:{_ts(11 * 60)}" and "stall" not in note["detail"].split(":")[0]
    assert "idle on purpose; this is not a stall" in note["detail"]
    assert notice_word(note["stall"], True) == "no worker capacity"
    (pushed,) = events_from_tick({}, [note])
    assert pushed.title == "No worker capacity"
    assert plan_stall_alarm(_facts(rows=_recorded([note]), **drained), [], _NOW + timedelta(hours=5)) == []
    # with capacity and nothing held, idle is not this check's business: ready work with free lanes is no-launch's
    assert plan_stall_alarm(_facts(held=[]), [], _NOW) == []


def test_held_work_with_no_capacity_is_still_the_held_alarm_and_says_so():
    (alarm,) = plan_stall_alarm(_facts(max_in_flight=0), [], _NOW)
    assert alarm["stall"].startswith("idle_held:") and "no worker capacity either" in alarm["detail"]


def test_progress_ends_the_spell_quietly_and_the_next_spell_is_told_again():
    rows = _recorded(plan_stall_alarm(_facts(), [], _NOW))
    later = _NOW + timedelta(hours=3)
    # a launch two hours ago moved the chair: the old spell closes unsaid, and the new one is told once it is an hour old
    actions = plan_stall_alarm(_facts(rows=rows, progress=_ts(120, later)), [], later)
    assert [(a["kind"], a["stall"], a.get("quiet")) for a in actions] == [
        ("stall_alarm", f"idle_held:{_ts(120, later)}", None), ("stall_clear", f"idle_held:{_ts(11 * 60)}", True)]
    assert events_from_tick({}, [actions[1]]) == []  # a quiet clear pushes nothing
    busy = plan_stall_alarm(_facts(rows=rows, live=1), [], later)
    assert [(a["kind"], a.get("quiet")) for a in busy] == [("stall_clear", True)]


def test_held_items_lists_tasks_then_initiatives_then_no_progress_holds():
    holds = initiative_holds([
        {"kind": "initiative", "task_id": "gamma", "initiative": "gamma", "state": "ready",
         "extra": {"held": True, "hold_reason": "needs you: waiting on a design call"}},
        {"kind": "intake", "task_id": "in-1", "state": "queued", "extra": {"initiative": "delta", "stage": "draft"}},
        {"kind": "intake", "task_id": "in-2", "state": "removed", "extra": {"initiative": "eps", "stage": "draft"}},
        {"kind": "initiative", "task_id": "zeta", "initiative": "zeta", "state": "ready", "extra": {}},
    ])
    assert [(h["item"], h["release"]) for h in holds] == [
        ("intake in-1 for delta", "cox route release in-1"), ("gamma", "cox route release gamma")]
    assert holds[1]["reason"] == "needs you: waiting on a design call"
    found = held_items(_HELD[:1], holds, {"gamma": {"reason": "dup"}, "omega": {"reason": "needs you: no progress"}})
    assert [h["item"] for h in found] == ["t1 in alpha", "intake in-1 for delta", "gamma", "omega"]
    assert found[-1]["release"] == "cox route release omega"
    (alarm,) = plan_stall_alarm(_facts(held=[], holds=holds), [], _NOW)
    assert "`cox route release gamma`" in alarm["detail"] and alarm["subject"] == "delta"


def test_worker_capacity_counts_the_local_cap_and_every_host_that_is_not_draining():
    dispatch = {"max_in_flight": 2, "live_runs": 0,
                "hosts": [{"name": "lane-a", "live_runs": 0, "capacity": 3}, {"name": "lane-b", "live_runs": 0}]}
    assert worker_capacity(dispatch, []) == 2 + 3 + 2
    assert worker_capacity(dispatch, [{"name": "lane-a", "state": "draining"}]) == 2 + 2
    assert worker_capacity({**dispatch, "max_in_flight": 0, "hosts": []}, []) == 0
    assert worker_capacity(None, []) == 0


def test_the_setting_is_a_cartridge_profile_key_with_a_one_hour_default():
    assert DEFAULT_IDLE_MINUTES == 60.0 and resolve_settings(None)["idle_minutes"] == 60.0
    profile, _ = _parse_profile("team: smoke\nchair:\n  idle_alarm_minutes: 90\n")
    assert resolve_settings(profile_settings(profile["chair"]))["idle_minutes"] == 90.0
    assert profile_settings({"stall_alarm": {"land_hours": 2}, "idle_alarm_minutes": 0}) == {"land_hours": 2, "idle_minutes": 0}
    assert profile_settings(None) == {}


def test_a_stall_message_id_is_the_same_for_the_same_action_and_differs_otherwise():
    one = stall_message_id("stall_alarm", "idle_held:x", "x")
    assert one == stall_message_id("stall_alarm", "idle_held:x", "x")
    assert one != stall_message_id("stall_clear", "idle_held:x", "x") != stall_message_id("stall_alarm", "idle_held:y", "y")


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


def _hold(ws: Path, initiative: str, task: str, reason: str) -> None:
    from harness import store_queue

    conn = open_store(run_store.store_url_for(ws / "runs"), "2026-01-01T00:00:00Z")
    try:
        store_queue.upsert(conn, {"kind": "task", "initiative": initiative, "task_id": task, "phase": "p1", "state": "blocked",
                                  "needs": [], "surfaces": [], "title": task, "body": "",
                                  "extra": {"id": task, "hold_reason": reason}})
    finally:
        conn.close()


def _tick(ws: Path) -> list[dict]:
    """One tick as the chair runs it, the real gather, plan, executor and recorder; returns this tick's alarm actions."""
    profile = {"workspace_dir": str(ws), "harness_dir": str(ws / "harness"), "chair": {"idle_alarm_minutes": 60}}
    run = cli._chair_run_deps(ws / "runs", profile, "chair", 1, "h", False, lambda line: None, ws / "profile.yaml", "store")
    now = datetime.now(UTC)
    facts = chair_facts.gather_facts(run.facts_deps, now)
    planned = plan_tick(facts, now)
    epoch = facts["lease"]["epoch"]
    record = chair_read_record.recorder(ws / "runs", lambda: epoch, lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        store=store_cli.runner(ws / "runs"), holder="chair")
    deps = chair_exec.edge_deps(ws / "runs", ws, "chair", 1, run_id=lambda a: "", repo_for=lambda a: "", record=record,
                                host="h", harness_python=sys.executable)
    chosen = [a for a in planned if a["kind"] in ("take_lease", "stall_alarm", "stall_clear")]
    results = chair_exec.perform(chosen, deps, lambda: epoch, False)
    assert all(r["status"] == "done" for r in results), results
    return [a for a in chosen if a["kind"] != "take_lease"]


def _courier(ws: Path) -> list[dict]:
    path = ws / "courier.jsonl"
    return courier.entries(path.read_text(encoding="utf-8")) if path.is_file() else []


def test_end_to_end_held_work_on_an_idle_chair_writes_one_courier_entry_naming_each_hold(ws):
    _hold(ws, "alpha", "t1", "needs you: platform defect A")
    _hold(ws, "beta", "t2", "needs you: platform defect B")
    assert _tick(ws) == []  # the lease, and the chair has only just started watching
    stamp = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    chair_read_record.recorder(ws / "runs", lambda: 0, lambda: stamp, store=store_cli.runner(ws / "runs"), holder="chair")(
        {"kind": "housekeeping", "status": "done", "reason": ""})  # it has watched, idle, for two hours
    (raised,) = _tick(ws)
    assert raised["stall"] == f"idle_held:{stamp}"
    (entry,) = _courier(ws)
    assert entry["event"] == "stall_raised" and str(entry["note"]).startswith("stall: the chair is idle with work held")
    assert entry["id"] == stall_message_id("stall_alarm", raised["stall"], stamp)
    assert [h["release"] for h in entry["fields"]["held"]] == [
        "cox route edit t1 --initiative alpha --state ready", "cox route edit t2 --initiative beta --state ready"]
    assert _tick(ws) == [] and _tick(ws) == []  # told once: the recorded row marks the spell told
    assert len(_courier(ws)) == 1
