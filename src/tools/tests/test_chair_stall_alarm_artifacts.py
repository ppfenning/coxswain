"""The artifacts-missing check of the stall alarm: a run whose step rows are marked `artifacts_error` raises
`artifacts_missing:<run>` once, from the chair, and the reader finds the marked rows in a real store."""

from datetime import UTC, datetime, timedelta

from agent_tools.chair_read_artifacts import read_marked_steps
from agent_tools.chair_stall_alarm import (
    MISSING_WINDOW,
    missing_runs,
    observe,
    plan_alarms,
    resolve_settings,
    stall_message_id,
    told_counts,
)

_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
_GATE = {"free_lanes": 0, "ready": 0, "subject": "", "held": frozenset(), "idle_raised": False, "paused": False,
         "busy": True, "capacity": 1, "held_items": [], "progress_at": None}


def _ts(minutes_ago: float) -> str:
    return (_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(run, minutes_ago, error="OSError: bucket down", initiative="ship"):
    result = {"patch_ok": True, **({"artifacts_error": error} if error else {})}
    return {"run_id": run, "initiative": initiative, "finished_at": _ts(minutes_ago), "result_json": result}


def _facts(missing, opened=None, told=None):
    return {
        "settings": resolve_settings(None), "last_land_at": None, "open_initiatives": {}, "last_launch_at": None,
        "watch_since": _ts(600), "heads": {}, "streaks": [], "open": opened or {}, "told": told or {},
        "stranded": {}, "holds": [], "artifacts_missing": missing,
    }


def _plan(facts, paused=False):
    return plan_alarms(observe(facts, {**_GATE, "paused": paused}, _NOW), facts, _NOW, paused)


def test_missing_runs_takes_each_marked_runs_first_error_within_the_window():
    rows = [_row("ship-1", 5, "second"), _row("ship-1", 10, "first"), _row("ship-2", 3, error=None),
            _row("ship-3", MISSING_WINDOW.total_seconds() / 60 + 1)]
    assert missing_runs(rows, _NOW) == {"ship-1": {"initiative": "ship", "error": "first", "at": _ts(10)}}


def test_a_marked_run_raises_one_alarm_naming_the_run_and_its_error():
    (action,) = _plan(_facts(missing_runs([_row("ship-1", 10)], _NOW)))
    assert (action["kind"], action["stall"], action["subject"], action["since"], action["seq"]) == (
        "stall_alarm", "artifacts_missing:ship-1", "ship", _ts(10), 0)
    assert "ship-1 could not upload step artifacts" in action["detail"] and "bucket down" in action["detail"]


def test_a_raised_alarm_for_a_run_still_marked_plans_nothing_more_even_under_the_hard_stop():
    opened = {"artifacts_missing:ship-1": {"since": _ts(10), "detail": "d", "subject": "ship"}}
    missing = missing_runs([_row("ship-1", 10)], _NOW)
    assert _plan(_facts(missing, opened)) == []
    assert _plan(_facts(missing, opened), paused=True) == []


def test_the_alarm_closes_quietly_once_the_runs_rows_age_out_of_the_window():
    opened = {"artifacts_missing:ship-1": {"since": _ts(10), "detail": "d", "subject": "ship"}}
    (action,) = _plan(_facts({}, opened))
    assert (action["kind"], action["stall"], action.get("quiet")) == ("stall_clear", "artifacts_missing:ship-1", True)


def test_a_retried_raise_keeps_its_courier_id_and_each_run_has_its_own():
    docs = [{"kind": "stall_alarm", "status": "failed", "stall": "artifacts_missing:ship-1"}]
    (first,) = _plan(_facts(missing_runs([_row("ship-1", 10)], _NOW), told=told_counts(docs)))
    other = _plan(_facts(missing_runs([_row("ship-2", 10)], _NOW)))[0]
    ids = {stall_message_id(a["kind"], a["stall"], a["since"], a["seq"]) for a in (first, other)}
    assert first["seq"] == 0 and len(ids) == 2


def test_the_reader_finds_the_marked_rows_in_a_real_store(tmp_path, monkeypatch):
    from harness.store_migrate import open_store
    from harness.store_steps import put_step, step_row

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path / "no-profile.yaml"))
    conn = open_store(f"sqlite:///{runs_dir / 'cox.db'}", _ts(0))
    for step, error in (("task/t1/build", "OSError: down"), ("task/t1/plan", None)):
        result = {"artifacts_error": error} if error else {}
        put_step(conn, step_row(run_id="ship-1", step=step, initiative="ship", phase="p1", task_id="t1",
                                kind=step.rpartition("/")[2], round_=1, ok=True, verdict="", result=result,
                                finished_at=_ts(5)))
    conn.close()
    assert [(r["run_id"], r["result_json"]) for r in read_marked_steps(runs_dir, _NOW)] == [
        ("ship-1", {"artifacts_error": "OSError: down"})]
    assert read_marked_steps(tmp_path / "no-store", _NOW) == []
