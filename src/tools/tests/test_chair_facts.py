import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_tools import chair_smoke, pacing, queue_rows, run_store
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import (
    FactsDeps,
    dispatch_facts,
    forge_review_prs,
    gather_facts,
    harness_failures,
    host_env_from_rows,
    idle_stall_inputs,
    initiative_facts,
    land_refusals_from_actions,
    lease_facts,
    limits_facts,
    new_missing_repos,
    quarantine_facts,
    repo_dir_name,
    resolve_idle_stall_minutes,
    stale_checkout_keys,
    todo_initiative_facts,
    waiting_reported_from_actions,
    windowed_actions,
)
from agent_tools.chair_idle_stall import diagnose, is_stalled
from agent_tools.chair_land_backoff import backoff_for
from agent_tools.chair_plan_fill import best_host, host_free_slots
from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_read_attempts import with_stored_rescues
from agent_tools.chair_read_record import recorder
from agent_tools.chair_read_stale import read_chair_actions
from agent_tools.chair_types import Facts
from agent_tools.usage_window import WeeklyReset, weekly_window_start

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)
MINE = lease_holder("s", 7, "h")
HARNESS = {"run": "i-1", "phase": "p1", "task": "a", "cause": "harness"}
QUARANTINED = {"initiative": "i", "phase": "p1", "task": "a"}
# The keys `runs_stranded._row` emits, and no others: a real stranded row carries no initiative.
STRANDED = {"run": "i-3", "task": "s", "phase": "p1", "branch": "i/s", "remedy": None}
BARE = {"task_id": "a", "initiative": "i", "has_patch": False, "rescue_failed": False}
IDLE_STALL = {
    "free_lanes": 2, "ready": 1, "queued": 0, "last_progress_at": "2026-09-25T11:44:00Z", "stall_minutes": 15,
    "hosts": [{"host": "h2", "ok": False, "detail": "ssh timed out"}], "empty_stubs": [], "lands_waiting": [],
    "blocked_ready": [], "open_signature": None, "open_diagnosis": None,
}


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def _deps(
    weekly_spent: float = 10.0,
    attempts: tuple[dict, ...] = (HARNESS,),
    live: tuple[str, ...] = (),
    quarantined: tuple[dict, ...] = (QUARANTINED,),
    stranded: tuple[dict, ...] = (STRANDED,),
    has_patch: bool = False,
) -> FactsDeps:
    docket = {
        "initiatives": [{"id": "i", "started": True, "ready_tasks": [{"id": "a", "phase": "p1", "needs": []}], "landed": ["z"]}],
        "busy_lanes": 1,
        "max_in_flight": 2,
    }
    return FactsDeps(
        lease=lambda: {"holder": MINE, "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(1.0, 5),
        weekly=lambda: _window(weekly_spent, 168),
        policy=lambda: POLICY,
        docket=lambda: docket,
        approved=lambda: [{"id": "a", "initiative": "i", "repo": "r", "phase": "p", "phase_done": True, "needs": [], "run": "i-1", "needs_fetch": True}],
        quarantined=lambda: quarantined,
        stranded=lambda: stranded,
        attempts=lambda: attempts,
        has_patch=lambda initiative, task: has_patch,
        live_initiatives=lambda: live,
        intake=lambda: ["old", "new"],
        work_store_ready=lambda: True,
        sources_configured=lambda: False,
        session="s",
        pid=7,
        host="h",
        idle_stall=lambda n: IDLE_STALL,
    )


def _work_row(kind: str, initiative: str, task_id: str, state: str) -> dict:
    return {"kind": kind, "initiative": initiative, "task_id": task_id, "phase": "p1", "state": state}


def test_an_absent_reader_yields_an_empty_value_and_never_raises():
    """An absent reader yields an empty value and never raises."""
    facts = gather_facts(_deps(), NOW)
    keys = (
        "todo_initiatives", "remote_unfetched", "schema_deaths", "landing", "handoff_stops", "review_prs",
        "newest_run_host", "home", "run_hosts", "last_housekeeping_at", "housekeeping_hours", "stale_days",
        "drafts", "missing_repos", "run_exited", "lost_runs", "pid_probe", "login_hosts", "host_env",
        "env_stale_reported", "stranded", "phase_branches",
    )
    got = {key: facts[key] for key in keys}
    got["limits_sources"] = (facts["limits"]["window_source"], facts["limits"]["weekly_source"])
    assert got == {
        "todo_initiatives": [], "remote_unfetched": {}, "schema_deaths": {}, "landing": [], "handoff_stops": [],
        "review_prs": [], "newest_run_host": {}, "home": {}, "run_hosts": {}, "last_housekeeping_at": None,
        "housekeeping_hours": 24, "stale_days": 7, "drafts": 0, "missing_repos": [], "run_exited": {},
        "lost_runs": {}, "pid_probe": {}, "login_hosts": [], "host_env": {}, "env_stale_reported": [],
        "stranded": [], "phase_branches": [], "limits_sources": ("est", "est"),
    }


def test_a_todo_initiative_lists_its_task_states_in_row_order():
    rows = [
        _work_row("initiative", "i", "i", "todo"),
        _work_row("task", "i", "a", "done"),
        _work_row("task", "i", "b", "approved"),
        _work_row("task", "i", "c", "todo"),
    ]
    assert todo_initiative_facts(rows) == [{"id": "i", "task_states": ["done", "approved", "todo"]}]


def test_intake_rows_and_other_initiatives_tasks_are_left_out_of_task_states():
    rows = [
        _work_row("intake", "i", "n", "queued"),
        _work_row("initiative", "i", "i", "todo"),
        _work_row("task", "i", "a", "done"),
        _work_row("task", "j", "x", "ready"),
    ]
    assert todo_initiative_facts(rows) == [{"id": "i", "task_states": ["done"]}]


def test_a_todo_initiative_with_no_task_rows_has_an_empty_task_states_list():
    assert todo_initiative_facts([_work_row("initiative", "i", "i", "todo")]) == [{"id": "i", "task_states": []}]


def test_an_initiative_that_is_not_todo_is_absent():
    rows = [_work_row("initiative", "i", "i", "done"), _work_row("task", "i", "a", "done")]
    assert todo_initiative_facts(rows) == []


def test_a_work_rows_callables_rows_appear_under_todo_initiatives():
    rows = [_work_row("initiative", "i", "i", "todo"), _work_row("task", "i", "a", "ready"), _work_row("task", "i", "b", "todo")]
    deps = replace(_deps(), work_rows=lambda: rows)
    assert gather_facts(deps, NOW)["todo_initiatives"] == [{"id": "i", "task_states": ["ready", "todo"]}]


def test_a_remote_unfetched_callables_mapping_appears_under_remote_unfetched():
    deps = replace(_deps(), remote_unfetched=lambda: {"i": "i-1"})
    assert gather_facts(deps, NOW)["remote_unfetched"] == {"i": "i-1"}


def test_gather_facts_carries_a_tuning_entry_with_the_meter_and_the_last_tuned_time():
    deps = replace(_deps(weekly_spent=25.0), tuning=lambda now: ([], "2026-09-24T00:00:00Z"), lane_bounds=lambda: {"": (1, 4)})
    tuning = gather_facts(deps, NOW)["tuning"]
    assert tuning["last_tuned_at"] == "2026-09-24T00:00:00Z"
    assert tuning["meter"] == {"weekly_fraction_used": 0.25, "week_elapsed_fraction": 1 / 168}
    assert tuning["hosts"] == {"": {"lanes": 2, "min_lanes": 1, "max_lanes": 4}}
    assert gather_facts(_deps(), NOW)["tuning"] == {
        "stats": [], "meter": {"weekly_fraction_used": 0.1, "week_elapsed_fraction": 1 / 168}, "hosts": {}, "last_tuned_at": None,
    }


def test_a_schema_deaths_callables_mapping_appears_under_schema_deaths():
    deps = replace(_deps(), schema_deaths=lambda: {"x": ["x-2", "x-1"]})
    assert gather_facts(deps, NOW)["schema_deaths"] == {"x": ["x-2", "x-1"]}


def test_a_landing_callables_list_appears_under_landing():
    landing = [{"initiative": "i", "phase": "p1", "repo": "/r"}]
    assert gather_facts(replace(_deps(), landing=lambda: landing), NOW)["landing"] == landing


def test_a_handoff_stops_callables_rows_appear_under_handoff_stops():
    rows = [{"initiative": "i", "task_id": "a", "state": "ready", "reason": "r", "surfaces": ["a.py"], "widenings": 0}]
    assert gather_facts(replace(_deps(), handoff_stops=lambda: rows), NOW)["handoff_stops"] == rows


REVIEW_PR = {
    "initiative": "i", "phase": "p1", "task_id": "a", "repo": "r",
    "url": "https://example.test/pr/1", "state": "open", "merged_at": None,
}


def _review_runs_dir(tmp_path):
    path = tmp_path / "i-1" / "tasks" / "p1" / "a.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "approved", "review_pr": REVIEW_PR["url"], "repo": "r"}), encoding="utf-8")
    return str(tmp_path)


def test_a_review_prs_callables_list_appears_under_review_prs():
    deps = replace(_deps(), review_prs=lambda: [REVIEW_PR])
    assert gather_facts(deps, NOW)["review_prs"] == [REVIEW_PR]


def test_a_fake_forge_reporting_merged_gives_that_entry_with_state_merged(tmp_path):
    fake = SimpleNamespace(pr_state=lambda url: {"state": "merged", "merged_at": "2026-10-04T00:00:00Z"})
    rows = forge_review_prs(_review_runs_dir(tmp_path), "fake", lambda name: fake)
    assert rows == [{
        "initiative": "i", "phase": "p1", "task_id": "a", "repo": "r",
        "url": REVIEW_PR["url"], "state": "merged", "merged_at": "2026-10-04T00:00:00Z", "run": "i-1",
    }]


def test_a_missing_forge_gives_state_unknown_for_every_entry(tmp_path):
    rows = forge_review_prs(_review_runs_dir(tmp_path), "nope", lambda name: None)
    assert [(r["url"], r["state"]) for r in rows] == [(REVIEW_PR["url"], "unknown")]


def test_a_newest_run_host_callables_mapping_appears_under_newest_run_host():
    deps = replace(_deps(), newest_run_host=lambda: {"i": "host-a"})
    assert gather_facts(deps, NOW)["newest_run_host"] == {"i": "host-a"}


def test_home_maps_an_initiative_with_an_unlanded_approved_task_to_its_newest_run_host():
    deps = replace(_deps(), newest_run_host=lambda: {"i": "host-a"})
    assert gather_facts(deps, NOW)["home"] == {"i": "host-a"}


def test_run_hosts_holds_a_remote_run_and_drops_a_local_one():
    deps = replace(_deps(), run_hosts=lambda: {"run-1": "host-a", "run-2": ""})
    assert gather_facts(deps, NOW)["run_hosts"] == {"run-1": "host-a"}


def test_home_holds_an_initiative_homed_on_omarchy():
    deps = replace(_deps(), newest_run_host=lambda: {"i": "host-b"})
    assert gather_facts(deps, NOW)["home"] == {"i": "host-b"}


def test_a_history_callable_returning_a_timestamp_is_held_as_last_housekeeping_at():
    deps = replace(_deps(), history=lambda: "2026-09-25T00:00:00Z")
    assert gather_facts(deps, NOW)["last_housekeeping_at"] == "2026-09-25T00:00:00Z"


def test_a_housekeeping_hours_callable_returning_6_gives_6_point_0():
    deps = replace(_deps(), housekeeping_hours=lambda: 6)
    assert gather_facts(deps, NOW)["housekeeping_hours"] == 6.0


def test_a_fake_stale_candidates_callable_returning_two_rows_gives_facts_holding_them_unchanged():
    rows = [{"initiative": "i", "task_id": "a"}, {"initiative": "i", "task_id": "b"}]
    deps = replace(_deps(), stale_candidates=lambda now: rows)
    assert gather_facts(deps, NOW)["stale_candidates"] == rows


def test_a_fake_stall_candidates_callable_returning_two_rows_gives_facts_holding_them_unchanged():
    rows = [{"run": "i-1", "initiative": "i"}, {"run": "i-2", "initiative": "i"}]
    deps = replace(_deps(), stall_candidates=lambda now: rows)
    assert gather_facts(deps, NOW)["stall_candidates"] == rows


def test_a_stale_days_callable_returning_10_gives_stale_days_10():
    deps = replace(_deps(), stale_days=lambda: 10)
    assert gather_facts(deps, NOW)["stale_days"] == 10


def test_a_host_load_ceiling_of_0_8_gives_the_fact_0_8():
    deps = replace(_deps(), host_load_ceiling=lambda: 0.8)
    assert gather_facts(deps, NOW)["host_load_ceiling"] == 0.8


def test_no_reader_gives_no_host_load_ceiling_key():
    assert "host_load_ceiling" not in gather_facts(_deps(), NOW)


@pytest.mark.parametrize("raw", [None, 0, 0.0, -0.5, "abc", True, float("nan"), float("inf"), [0.8]])
def test_an_absent_zero_or_malformed_host_load_ceiling_gives_no_key(raw):
    deps = replace(_deps(), host_load_ceiling=lambda: raw)
    assert "host_load_ceiling" not in gather_facts(deps, NOW)


def test_weekly_spend_of_85_percent_is_a_hard_stop_with_no_launches():
    limits = gather_facts(_deps(weekly_spent=85.0), NOW)["limits"]
    assert limits == {
        "hard_stop": True,
        "weekly_fraction": 0.85,
        "hard_stop_fraction": 0.85,
        "launch_cap": 0,
        "go_degraded": False,
        "five_hour_fraction": 0.01,
        "window_start_day": "Fri 07:00 EDT",
        "window_source": "est",
        "weekly_source": "est",
        "smoke_hold": None,
        "lane_fraction": 1.0,
        "weekly_target_fraction": 0.005357142857142857,
        "weekly_pace": "not_judged",
    }


def test_a_meter_backed_window_source_callable_reports_meter_in_limits():
    deps = replace(_deps(), window_source=lambda: "meter")
    assert gather_facts(deps, NOW)["limits"]["window_source"] == "meter"


def test_a_meter_backed_weekly_source_callable_reports_meter_in_limits():
    deps = replace(_deps(), weekly_source=lambda: "meter")
    assert gather_facts(deps, NOW)["limits"]["weekly_source"] == "meter"


def test_five_hour_fraction_is_the_assessments_spent_fraction():
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    assert limits_facts(assessment, POLICY, _window(10.0, 168), 2)["five_hour_fraction"] == 0.42


def test_a_saturday_night_eastern_reset_reads_as_saturday_not_as_the_utc_sunday():
    # The reset's start is 2026-11-01 03:00 UTC, a Sunday in UTC; the operator configured Saturday 23:00 Eastern.
    reset = WeeklyReset(weekday=5, hour=23, minute=0, tz="America/New_York")
    start = weekly_window_start(datetime(2026, 11, 2, 15, 0, tzinfo=UTC), reset)
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    window = pacing.Window(start, start + timedelta(days=7), 10.0, 100.0, 0.0, 0)
    assert limits_facts(assessment, POLICY, window, 2)["window_start_day"] == "Sat 23:00 EDT"


def test_no_weekly_window_gives_no_window_start_day():
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    assert limits_facts(assessment, POLICY, None, 2)["window_start_day"] is None


def test_a_written_smoke_hold_surfaces_its_cause_and_land_on_limits_facts(tmp_path):
    record = {
        "land": {"repo": "acme/widgets", "pr": 7, "commit": "abc123"},
        "failing_command": ["cox", "route", "context"],
        "tail": "Traceback...",
        "cause": "smoke_failed",
    }
    chair_smoke.write_hold(str(tmp_path), record)
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    smoke_hold = limits_facts(assessment, POLICY, None, 2, runs_dir=str(tmp_path))["smoke_hold"]
    assert smoke_hold is not None
    assert smoke_hold["cause"] == "smoke_failed"
    assert smoke_hold["land"] == record["land"]


def test_an_empty_runs_dir_reports_no_smoke_hold(tmp_path):
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    assert limits_facts(assessment, POLICY, None, 2, runs_dir=str(tmp_path))["smoke_hold"] is None


def test_weekly_spend_under_the_fraction_launches_up_to_max_in_flight():
    limits = gather_facts(_deps(weekly_spent=84.0), NOW)["limits"]
    assert (limits["hard_stop"], limits["launch_cap"]) == (False, 2)


def test_two_harness_attempts_give_harness_failures_of_two():
    attempts = [HARNESS, {**HARNESS, "cause": "code"}, {**HARNESS, "run": "i-2"}, {**HARNESS, "task": "other"}]
    assert harness_failures(attempts, ("i", "p1", "a")) == 2


def test_a_task_id_repeated_in_another_phase_is_counted_apart():
    assert harness_failures([HARNESS, {**HARNESS, "phase": "p2"}], ("i", "p1", "a")) == 1


def test_harness_attempts_off_the_current_body_are_not_counted():
    attempts = [{**HARNESS, "on_current_body": False}, {**HARNESS, "run": "i-2", "on_current_body": False}]
    assert harness_failures(attempts, ("i", "p1", "a")) == 0


def test_a_harness_attempt_with_no_on_current_body_key_still_counts():
    assert harness_failures([HARNESS], ("i", "p1", "a")) == 1


def test_the_cause_and_the_count_come_from_the_same_attempts():
    (q,) = gather_facts(_deps(attempts=({**HARNESS, "cause": "code"}, HARNESS), stranded=()), NOW)["quarantines"]
    assert q == {**BARE, "cause": "harness", "harness_failures": 1}


def test_a_quarantined_task_with_no_live_run_gives_a_quarantine_and_an_initiative():
    facts = gather_facts(_deps(stranded=()), NOW)
    assert facts["quarantines"] == [{**BARE, "cause": "harness", "harness_failures": 1}]
    assert [i["id"] for i in facts["initiatives"]] == ["i"]


def test_a_quarantined_task_whose_initiative_has_a_live_run_gives_neither():
    facts = gather_facts(_deps(live=("i",)), NOW)
    assert facts["quarantines"] == []
    assert facts["initiatives"] == []


def test_a_stranded_row_takes_its_initiative_from_its_run_and_a_live_run_drops_it():
    def stranded(live: tuple[str, ...]) -> list:
        return gather_facts(_deps(quarantined=(), live=live), NOW)["quarantines"]

    # `tasks` is the deliberate shape change: a stranded entry lists its run's tasks with their remedies.
    assert stranded(()) == [
        {**BARE, "task_id": "s", "cause": "stranded", "harness_failures": 0, "tasks": [{"task": "s", "run": "i-3", "remedy": None}]},
    ]
    assert stranded(("i",)) == []


def _stranded_entries(*rows: dict, **kw) -> list:
    return quarantine_facts([], list(rows), [], set(), lambda i, t: False, **kw)


def test_an_expected_stranded_row_raises_no_entry_and_a_plain_one_beside_it_still_does():
    manual = {**STRANDED, "task": "m", "expected": True}
    plain = {**STRANDED, "task": "p", "expected": False}
    assert [q["task_id"] for q in _stranded_entries(manual)] == []
    assert [q["task_id"] for q in _stranded_entries(manual, plain)] == ["p"]


def test_two_stranded_tasks_of_one_run_give_one_entry_listing_both_with_their_remedies():
    a = {**STRANDED, "task": "a", "remedy": "land a"}
    b = {**STRANDED, "task": "b", "remedy": "land b"}
    (entry,) = _stranded_entries(a, b)
    assert (entry["task_id"], entry["cause"], entry["initiative"]) == ("a", "stranded", "i")
    assert entry["tasks"] == [{"task": "a", "run": "i-3", "remedy": "land a"}, {"task": "b", "run": "i-3", "remedy": "land b"}]


def test_a_single_stranded_task_gives_one_entry_with_its_original_remedy():
    (entry,) = _stranded_entries({**STRANDED, "remedy": "cox runs land i-3 --task s --repo /r"})
    assert entry["tasks"] == [{"task": "s", "run": "i-3", "remedy": "cox runs land i-3 --task s --repo /r"}]


def test_stranded_tasks_of_two_runs_give_two_entries():
    entries = _stranded_entries({**STRANDED, "run": "i-3", "task": "a"}, {**STRANDED, "run": "i-4", "task": "b"})
    assert [(q["task_id"], q["tasks"][0]["run"]) for q in entries] == [("a", "i-3"), ("b", "i-4")]


def test_a_task_that_is_also_an_open_quarantine_leaves_the_grouped_entry():
    opened = [{"initiative": "i", "phase": "p1", "task": "a"}]
    stranded = [{**STRANDED, "task": "a"}, {**STRANDED, "task": "b"}]
    entries = quarantine_facts(opened, stranded, [], set(), lambda i, t: False)
    assert [(q["task_id"], q["cause"]) for q in entries] == [("a", "quarantined"), ("b", "stranded")]
    assert [t["task"] for t in entries[1]["tasks"]] == ["b"]


def test_a_stranded_entry_carries_no_top_level_run_so_launch_history_ignores_it():
    (entry,) = _stranded_entries(STRANDED)
    assert "run" not in entry


def _stranded_causes(approved: list | None, phase: str | None = "p1") -> list[str]:
    stranded = [{"run": "i-3", "task": "s", "phase": phase}]
    return [q["cause"] for q in quarantine_facts([], stranded, [], set(), lambda i, t: False, approved=approved)]


def test_a_stranded_row_whose_task_is_not_approved_in_the_store_is_dropped():
    assert _stranded_causes([{"id": "other", "initiative": "i", "phase": "p1"}]) == []
    assert _stranded_causes([]) == []


def test_a_stranded_row_whose_task_is_approved_in_the_store_stays_stranded():
    assert _stranded_causes([{"id": "s", "initiative": "i", "phase": "p1"}]) == ["stranded"]


def test_a_stranded_row_with_no_phase_or_an_old_phase_for_an_approved_task_stays_stranded():
    # `runs_stranded` accepts a record with no phase, and one whose task moved phase; both name an approved task.
    approved = [{"id": "s", "initiative": "i", "phase": "p1"}]
    assert _stranded_causes(approved, phase=None) == ["stranded"]
    assert _stranded_causes(approved, phase="p0") == ["stranded"]


def test_omitting_the_approved_tasks_keeps_every_stranded_row():
    assert _stranded_causes(None) == ["stranded"]


def test_an_open_quarantine_with_a_real_cause_survives_an_approved_list_that_lacks_it():
    opened = [{"initiative": "i", "phase": "p1", "task": "a"}]
    attempts = [{"initiative": "i", "phase": "p1", "task": "a", "cause": "code"}]
    (q,) = quarantine_facts(opened, [], attempts, set(), lambda i, t: False, approved=[])
    assert q["cause"] == "code"


def test_a_fake_has_patch_gives_that_value_as_has_patch():
    (kept,) = gather_facts(_deps(has_patch=True, stranded=()), NOW)["quarantines"]
    (dropped,) = gather_facts(_deps(has_patch=False, stranded=()), NOW)["quarantines"]
    assert (kept["has_patch"], dropped["has_patch"]) == (True, False)


def test_a_rescue_failed_attempt_gives_rescue_failed_true_and_leaves_harness_failures_alone():
    (q,) = gather_facts(_deps(attempts=(HARNESS, {**HARNESS, "cause": "rescue_failed"}), stranded=()), NOW)["quarantines"]
    assert (q["rescue_failed"], q["harness_failures"]) == (True, 1)


def test_a_task_with_only_harness_attempts_gives_rescue_failed_false_and_harness_failures_of_one():
    (q,) = gather_facts(_deps(attempts=(HARNESS,), stranded=()), NOW)["quarantines"]
    assert (q["rescue_failed"], q["harness_failures"]) == (False, 1)


def test_an_approved_record_with_a_harness_attempt_and_a_kept_patch_plans_rescue():
    stranded = {**STRANDED, "run": "i-1", "task": "a"}
    facts = gather_facts(_deps(stranded=(stranded,), has_patch=True), NOW)
    assert [a["kind"] for a in plan_recover(facts) if a["kind"] in ("rescue", "retry", "needs_chair")] == ["rescue"]


def _kinds_with_stored_rescue(rescue_ts: str) -> list[str]:
    store = [{"run_id": "i-1", "task_id": "a", "phase_id": "p1", "ts": rescue_ts, "cause": "code"}]
    attempts = with_stored_rescues([{**HARNESS, "ts": "2026-09-10"}], store, {("i", "p1", "a"): "2026-09-10"})
    facts = gather_facts(_deps(attempts=tuple(attempts), stranded=(), has_patch=True), NOW)
    return [a["kind"] for a in plan_recover(facts) if a["kind"] in ("rescue", "retry", "needs_chair")]


def test_a_store_rescue_failed_row_on_the_current_body_sends_the_task_to_the_chair_not_rescue():
    assert _kinds_with_stored_rescue("2026-09-11") == ["needs_chair"]


def test_a_store_rescue_failed_row_older_than_the_current_bodys_first_attempt_still_plans_rescue():
    assert _kinds_with_stored_rescue("2026-09-09") == ["rescue"]


def test_a_failed_retry_reads_two_and_the_planner_hands_it_to_the_chair():
    def kinds(attempts: tuple[dict, ...]) -> list[str]:
        facts = gather_facts(replace(_deps(attempts=attempts), stranded=lambda: []), NOW)
        return [a["kind"] for a in plan_recover(facts) if a["kind"] in ("retry", "needs_chair")]

    assert kinds((HARNESS,)) == ["retry"]
    assert kinds((HARNESS, {**HARNESS, "run": "i-2"})) == ["needs_chair"]


def test_dispatch_facts_split_the_local_lanes_from_each_lane_hosts():
    facts = dispatch_facts({"max_in_flight": 4}, ["host-a"], {"": 3, "host-a": 1})
    assert facts["live_runs"] == 3
    assert facts["hosts"] == [{"name": "host-a", "live_runs": 1, "capabilities": []}]


def test_dispatch_facts_carry_a_hosts_own_capacity_only_when_it_has_one():
    facts = dispatch_facts({"max_in_flight": 4}, ["host-a", "pi"], {}, {"host-a": 8})
    assert facts["hosts"] == [
        {"name": "host-a", "live_runs": 0, "capacity": 8, "capabilities": []},
        {"name": "pi", "live_runs": 0, "capabilities": []},
    ]


def test_dispatch_facts_carries_weight_and_capabilities_from_a_hosts_row():
    facts = dispatch_facts({"max_in_flight": 4}, ["h1"], {}, weight={"h1": 2}, capabilities={"h1": ["go"]})
    assert facts["hosts"] == [{"name": "h1", "live_runs": 0, "weight": 2, "capabilities": ["go"]}]


def test_dispatch_facts_leaves_weight_out_and_capabilities_empty_with_no_row():
    facts = dispatch_facts({"max_in_flight": 4}, ["h1"], {})
    assert facts["hosts"] == [{"name": "h1", "live_runs": 0, "capabilities": []}]


def test_dispatch_facts_never_places_the_local_host_among_the_hosts():
    facts = dispatch_facts({"max_in_flight": 4}, ["host-b", "host-a"], {}, local="host-b")
    assert facts["hosts"] == [{"name": "host-a", "live_runs": 0, "capabilities": []}]


def _first_host(weights: dict[str, int]) -> str:
    """The host best_host picks first for host-a (capacity 4) listed before host-b (capacity 10), via dispatch_facts."""
    dispatch = dispatch_facts({"max_in_flight": 1}, ["host-a", "host-b"], {}, {"host-a": 4, "host-b": 10}, weights)
    return best_host(frozenset(), host_free_slots({"dispatch": dispatch}))[0]  # type: ignore[typeddict-item,index]


def test_hosts_with_no_table_weight_are_placed_by_capacity_through_dispatch_facts():
    assert _first_host({}) == "host-b"


def test_a_table_weight_of_one_is_explicit_and_beats_capacity_through_dispatch_facts():
    assert _first_host({"host-a": 1, "host-b": 1}) == "host-a"
    assert _first_host({"host-a": 3, "host-b": 1}) == "host-a"


def test_dispatch_facts_carry_local_lanes_only_when_it_is_not_any():
    carried = [dispatch_facts({"max_in_flight": 4}, [], {}, local_lanes=v).get("local_lanes") for v in ("decompose", "any")]
    assert [*carried, "local_lanes" in dispatch_facts({"max_in_flight": 4}, [], {})] == ["decompose", None, False]


def test_a_ready_tasks_requires_frontmatter_reaches_the_facts():
    def row(task: str, frontmatter: str) -> dict:
        parsed = queue_rows.parse_item("task", ("i", "p1", f"{task}.md"), f"---\n{frontmatter}---\n")
        assert parsed is not None
        return {**parsed, "holder": None, "epoch": 0, "expires_at": None}

    rows = [row("a", "state: ready\nrequires: [go]\n"), row("b", "state: ready\n")]
    facts = gather_facts(replace(_deps(), queue=lambda: rows), NOW)
    assert facts["initiatives"][0]["ready_tasks"] == [
        {"id": "a", "phase": "p1", "needs": [], "requires": ["go"]}, {"id": "b", "phase": "p1", "needs": [], "requires": []},
    ]


def test_initiative_facts_keeps_the_phase_of_a_ready_and_a_waiting_task():
    docket = {
        "initiatives": [
            {
                "id": "i", "started": False, "landed": [],
                "ready_tasks": [{"id": "a", "phase": "p1", "needs": []}],
                "waiting_tasks": [{"id": "b", "phase": "p2", "needs": ["x"], "requires": ["go"]}],
            }
        ]
    }
    (facts,) = initiative_facts(docket, set())
    assert facts["ready_tasks"] == [{"id": "a", "phase": "p1", "needs": [], "requires": []}]
    assert facts["waiting_tasks"] == [{"id": "b", "phase": "p2", "needs": ["x"], "requires": ["go"]}]


WAITING = {"kind": "waiting", "initiative": "a", "phase": "p1", "quarantined": 1, "awaiting_land": 0}


def test_one_waiting_row_holds_its_phase_quarantined_and_awaiting_land():
    assert waiting_reported_from_actions([{"ts": "1", **WAITING}]) == {"a": "p1|1|0"}


def test_two_waiting_rows_with_different_counts_hold_the_newer_key():
    rows = [{"ts": "2", **WAITING, "quarantined": 2, "awaiting_land": 1}, {"ts": "1", **WAITING}]
    assert waiting_reported_from_actions(rows) == {"a": "p1|2|1"}


def test_a_launch_of_the_same_initiative_after_a_waiting_row_removes_the_key():
    rows = [{"ts": "1", **WAITING}, {"ts": "2", "kind": "launch_epic", "initiative": "a"}]
    assert waiting_reported_from_actions(rows) == {}


def test_a_launch_of_another_initiative_leaves_the_waiting_key_alone():
    rows = [{"ts": "1", **WAITING}, {"ts": "2", "kind": "launch_epic", "initiative": "b"}]
    assert waiting_reported_from_actions(rows) == {"a": "p1|1|0"}


def test_lease_is_mine_only_for_this_holder_on_a_live_lease():
    def mine(**record: object) -> bool:
        return lease_facts({"epoch": 4, **record}, "s", 7, "h")["mine"]

    assert mine(holder=MINE) is True
    assert mine(holder=lease_holder("t", 8, "g")) is False
    assert mine(holder=MINE, released=True) is False
    assert mine(holder=MINE, stale=True) is False


def test_lease_facts_carries_session_held_only_when_the_record_has_it():
    assert lease_facts({"epoch": 4, "holder": MINE, "session_held": True}, "s", 7, "h")["session_held"] is True
    assert "session_held" not in lease_facts({"epoch": 4, "holder": MINE}, "s", 7, "h")


def test_gather_facts_fills_every_key_from_the_fakes():
    # merge_queue and host_load_ceiling are gathered only when configured, so these fakes configure them.
    queue = {"batch_size": 3, "wait_s": 90}
    deps = replace(_deps(), phase_records=lambda tasks: [], merge_queue=lambda: queue, host_load_ceiling=lambda: 0.8)
    facts = gather_facts(deps, NOW)
    # login_hosts is not yet declared on Facts: a later task adds it there once the login watch reads it.
    # tuning, landed_main and land_outcomes are all filled in gather_facts now.
    # steward is declared on Facts and read by the planner; the next task gathers it, until then it is absent.
    # live_phases is gathered only when the deps supply it, and these fakes do not.
    # previous_schedule_feed is read by the planner; no task gathers it yet.
    # merged_prs is read by plan_missed_closes and handled_keys by plan_recover; the facts edge gathers neither yet.
    declared = set(Facts.__annotations__) - {
        "steward", "live_phases", "schedule_inputs", "previous_schedule_feed", "merged_prs", "handled_keys",
    }
    assert set(facts) == declared | {"login_hosts"}
    assert facts["dispatch"] == {"max_in_flight": 2, "live_runs": 1, "hosts": []}
    assert facts["initiatives"][0]["landed"] == {"z"}
    assert facts["approved"][0]["phase_done"] is True
    assert facts["intake"] == ["old", "new"]


def test_a_fake_idle_stall_callable_gives_its_literal_unchanged_under_idle_stall():
    assert gather_facts(_deps(), NOW)["idle_stall"] == IDLE_STALL


def test_an_unset_idle_stall_minutes_resolves_to_15():
    assert resolve_idle_stall_minutes(None) == 15


def test_a_configured_idle_stall_minutes_overrides_15():
    assert resolve_idle_stall_minutes(20.0) == 20


def test_a_failed_host_with_free_lanes_a_ready_item_and_16_idle_minutes_diagnoses_as_a_host():
    backlog = {"free_lanes": 2, "ready": 1, "queued": 0, "last_progress_at": "2026-09-25T11:44:00Z", "empty_stubs": [], "blocked_ready": []}
    hosts = [{"host": "h2", "ok": False, "detail": "ssh timed out"}]
    inputs = idle_stall_inputs(backlog, hosts, [], (None, None), resolve_idle_stall_minutes(None))
    facts = gather_facts(replace(_deps(), idle_stall=lambda n: inputs), NOW)
    assert (is_stalled(facts["idle_stall"], NOW), diagnose(facts["idle_stall"])["signature"]) == (True, "host:h2")


def test_a_fake_drafts_callable_returning_3_gives_drafts_3():
    assert gather_facts(replace(_deps(), drafts=lambda: 3), NOW)["drafts"] == 3


def test_new_missing_repos_drops_paths_already_reported():
    assert new_missing_repos(["/b", "/a", "/a"], {"/b"}) == ["/a"]


def test_a_fake_missing_repos_callable_returning_one_path_gives_facts_holding_it():
    assert gather_facts(replace(_deps(), missing_repos=lambda: ["/x"]), NOW)["missing_repos"] == ["/x"]


def test_a_reported_path_leaves_the_facts_empty_and_raises_no_quarantine():
    deps = replace(_deps(), missing_repos=lambda: ["/x"], reported_repos=lambda: {"/x"})
    facts = gather_facts(deps, NOW)
    assert facts["missing_repos"] == []
    assert facts["quarantines"] == gather_facts(_deps(), NOW)["quarantines"]


def test_a_fake_run_exited_callable_appears_under_run_exited():
    assert gather_facts(replace(_deps(), run_exited=lambda: {"i": True}), NOW)["run_exited"] == {"i": True}


def test_a_fake_lost_runs_callable_appears_under_lost_runs():
    assert gather_facts(replace(_deps(), lost_runs=lambda: {"i": "i-2"}), NOW)["lost_runs"] == {"i": "i-2"}


def test_a_fake_pid_probe_callable_appears_under_pid_probe():
    probe = {
        "up": {"alive": True, "last_beat_at": "2026-10-05T12:00:00+00:00"},
        "down": {"alive": False, "last_beat_at": "2026-10-05T12:01:00+00:00"},
    }
    assert gather_facts(replace(_deps(), pid_probe=lambda: probe), NOW)["pid_probe"] == probe


def test_a_fake_hosts_callables_return_value_appears_verbatim_under_login_hosts():
    rows = [{"name": "h1", "state": "needs_login", "versions_json": "{}"}]
    assert gather_facts(replace(_deps(), hosts=lambda: rows), NOW)["login_hosts"] == rows


ENV = {"coxswain-tools": {"lock_hash": "a", "synced_hash": "b"}}


def test_a_hosts_row_with_a_checkout_env_record_appears_under_its_host_name():
    rows = [{"name": "host-a", "versions_json": json.dumps({"checkout_env": ENV})}]
    assert host_env_from_rows(rows) == {"host-a": ENV}


def test_a_hosts_row_without_the_checkout_env_key_gives_an_empty_dict():
    assert host_env_from_rows([{"name": "host-a", "versions_json": '{"cox": "1"}'}]) == {"host-a": {}}


def test_an_unparseable_versions_json_gives_an_empty_dict_and_raises_nothing():
    assert host_env_from_rows([{"name": "host-a", "versions_json": "{not json"}]) == {"host-a": {}}


def test_repo_dir_name_is_the_last_path_component_of_a_repo():
    assert repo_dir_name("/home/user/repos/coxswain-tools/") == "coxswain-tools"


def test_an_initiative_row_keeps_its_repo_path_and_gains_its_repo_directory_name():
    docket = {"initiatives": [{"id": "i", "started": False, "landed": [], "ready_tasks": []}]}
    (facts,) = initiative_facts(docket, set(), None, {"i": "/home/user/repos/coxswain-tools"})
    assert (facts["repo"], facts["repo_dir"]) == ("/home/user/repos/coxswain-tools", "coxswain-tools")


def test_a_stale_checkout_needs_chair_row_gives_its_host_and_repo_key():
    stale = {"kind": "needs_chair", "cause": "stale_checkout", "host": "host-a", "repo": "coxswain-tools"}
    lapsed = {"kind": "needs_chair", "cause": "login_lapsed", "host": "host-a"}
    rows = [{"action_json": json.dumps(stale)}, {"action_json": json.dumps(lapsed)}]
    assert stale_checkout_keys(rows) == ["host-a|coxswain-tools"]


def _row(initiative: str, task_id: str, state: str, **extra: object) -> dict:
    return {
        "kind": "task", "initiative": initiative, "phase": "p1", "task_id": task_id, "state": state,
        "needs": [], "holder": None, "expires_at": None, "body": "", "extra": {},
        **extra,
    }


ROWS = [
    _row("i", "a", "ready"),
    # A quarantined-state row sits in a different initiative than "a": `route.STATES` has no "quarantined" member,
    # so a quarantined row in the same initiative as a ready task would zero out that initiative's own readiness.
    _row("j", "b", "quarantined", body="the body", extra={"attempts": [{"run": "j-9", "cause": "code", "ts": "2026-09-01"}]}),
    _row("i", "c", "approved"),
    {"kind": "intake", "initiative": "intake", "phase": "", "task_id": "q1", "state": "queued", "extra": {"initiative": None}},
]
STRANDED_RECORDS = [
    {"run": "i-5", "task": "c", "phase": "p1", "initiative": "i", "branch": "i/c", "repo": "r",
     "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"}, "landed": False},
]


def test_rows_drive_ready_intake_quarantined_and_stranded_facts_with_files_ignored():
    deps = replace(
        _deps(),
        queue=lambda: ROWS,
        stranded_records=lambda: STRANDED_RECORDS,
        attempts=lambda: ({"run": "j-9", "phase": "p1", "task": "b", "initiative": "j", "cause": "code"},),
        docket=lambda: {"initiatives": [{"id": "z", "started": False, "ready_tasks": [], "landed": []}], "busy_lanes": 1, "max_in_flight": 2},
        intake=lambda: ["zzz-intake.md"],
        quarantined=lambda: ({"initiative": "z", "phase": "p9", "task": "z"},),
        stranded=lambda: ({"run": "z-1", "task": "z", "phase": "p9", "branch": "z/z", "remedy": None},),
    )
    facts = gather_facts(deps, NOW)
    # "j" also appears (its quarantined-state row leaves it unlaunchable): the fixed point is "i"'s own facts.
    initiative_i = next(i for i in facts["initiatives"] if i["id"] == "i")
    assert initiative_i == {
        "id": "i", "started": True, "ready_tasks": [{"id": "a", "phase": "p1", "needs": [], "requires": []}], "waiting_tasks": [],
        "landed": {"c"}, "land_at": "ticket",  # c is approved, which a ticket initiative counts as landed
    }
    assert facts["intake"] == ["intake/q1.md"]
    assert facts["quarantines"] == [
        {"task_id": "b", "initiative": "j", "cause": "code", "harness_failures": 0, "has_patch": False, "rescue_failed": False},
        {"task_id": "c", "initiative": "i", "cause": "stranded", "harness_failures": 0, "has_patch": False, "rescue_failed": False,
         "tasks": [{"task": "c", "run": "i-5", "remedy": "cox runs land i-5 --task c --repo r"}]},
    ]


def _store_deps(task_state: str):
    """Store rows with `d` done (so "i" is started), `a` ready and `c` at `task_state`, and a record that says `c` was approved."""
    rows = [_row("i", "d", "done"), _row("i", "a", "ready"), _row("i", "c", task_state)]
    return replace(_deps(), queue=lambda: rows, stranded_records=lambda: STRANDED_RECORDS, quarantined=lambda: (), attempts=lambda: ())


def test_a_stranded_record_for_a_task_the_store_has_at_ready_makes_no_quarantine_and_the_initiative_relaunches():
    facts = gather_facts(_store_deps("ready"), NOW)
    assert facts["quarantines"] == []
    actions = plan_recover(facts)
    assert {"kind": "relaunch", "initiative": "i"} in actions
    assert not [a for a in actions if a["kind"] == "needs_chair"]


def test_a_stranded_record_for_a_task_the_store_has_approved_still_makes_the_stranded_quarantine():
    (q,) = gather_facts(_store_deps("approved"), NOW)["quarantines"]
    assert (q["task_id"], q["cause"]) == ("c", "stranded")


def test_a_stranded_record_in_a_manual_repo_raises_no_quarantine_in_store_mode(tmp_path):
    manual = tmp_path / "manual"
    (manual / ".agent").mkdir(parents=True)
    (manual / ".agent" / "cartridge.yaml").write_text("land: manual\n")
    auto = tmp_path / "auto"
    auto.mkdir()

    def stranded_causes(repo: object) -> list[str]:
        deps = replace(_store_deps("approved"), stranded_records=lambda: [{**STRANDED_RECORDS[0], "repo": str(repo)}])
        return [q["cause"] for q in gather_facts(deps, NOW)["quarantines"]]

    assert stranded_causes(auto) == ["stranded"]
    assert stranded_causes(manual) == []


def test_a_run_with_one_approved_and_one_unapproved_stranded_task_still_plans_one_needs_chair():
    # `_deps` approves only "a": the run's lead task is "b", so recovery does not read the whole entry as waiting.
    rows = ({**STRANDED, "task": "a"}, {**STRANDED, "task": "b"})
    facts = gather_facts(_deps(quarantined=(), stranded=rows), NOW)
    (entry,) = facts["quarantines"]
    assert (entry["task_id"], [t["task"] for t in entry["tasks"]]) == ("b", ["a", "b"])
    assert [a["kind"] for a in plan_recover(facts) if a["kind"] == "needs_chair"] == ["needs_chair"]


def test_a_run_whose_stranded_tasks_are_all_approved_keeps_the_first_task_as_lead():
    entries = quarantine_facts([], [{**STRANDED, "task": "a"}, {**STRANDED, "task": "b"}], [], set(), lambda i, t: False,
                               waiting={("i", "a"), ("i", "b")})
    assert [q["task_id"] for q in entries] == ["a"]


def test_without_store_rows_a_stranded_row_missing_from_the_approved_list_is_kept():
    # The file readers carry no store state to filter by; `_deps` approves only "a", so "s" is not in its list.
    assert [q["task_id"] for q in gather_facts(_deps(quarantined=()), NOW)["quarantines"]] == ["s"]


def test_an_empty_queue_read_falls_back_to_the_file_readers():
    deps = replace(_deps(), queue=lambda: [])
    assert gather_facts(deps, NOW) == gather_facts(_deps(), NOW)


def test_a_claimed_task_is_absent_from_the_ready_facts():
    rows = [
        {**_row("i", "a", "ready"), "holder": "other", "expires_at": "2026-09-26T00:00:00Z"},
        _row("i", "b", "ready"),
    ]
    deps = replace(_deps(), queue=lambda: rows)
    facts = gather_facts(deps, NOW)
    assert [t["id"] for t in facts["initiatives"][0]["ready_tasks"]] == ["b"]


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _refusal(at: datetime, status: str = "refused") -> dict:
    return {"ts": _stamp(at), "kind": "land_phase", "status": status, "initiative": "i", "phase": "p", "run": "i-1"}


def test_attempts_count_only_the_refusals_inside_the_last_24_hours():
    offsets = [n * 272 for n in range(1267)]
    rows = [_refusal(NOW - timedelta(seconds=s)) for s in offsets]
    [refusal] = land_refusals_from_actions(rows, NOW)
    assert refusal["attempts"] == sum(1 for s in offsets if s < 86400) == 318
    assert refusal["last_refused_at"] == _stamp(NOW)


def test_a_refusal_exactly_at_the_horizon_is_dropped_and_one_second_inside_is_kept():
    rows = [
        _refusal(NOW - timedelta(hours=24)),
        _refusal(NOW - timedelta(hours=24) + timedelta(seconds=1)),
        _refusal(NOW + timedelta(seconds=1), "failed"),
    ]
    [refusal] = land_refusals_from_actions(rows, NOW)
    assert refusal["attempts"] == 1
    assert land_refusals_from_actions(rows[:1], NOW) == []


def test_a_landed_row_inside_the_horizon_still_resets_the_count():
    rows = [
        _refusal(NOW - timedelta(hours=5)),
        _refusal(NOW - timedelta(hours=4), "landed"),
        _refusal(NOW - timedelta(hours=3), "failed"),
    ]
    [refusal] = land_refusals_from_actions(rows, NOW)
    assert refusal["attempts"] == 1
    assert refusal["last_refused_at"] == _stamp(NOW - timedelta(hours=3))


def test_a_refusal_carries_the_head_of_its_newest_unlanded_row():
    rows = [
        {**_refusal(NOW - timedelta(hours=3)), "head": "aaa111"},
        {**_refusal(NOW - timedelta(hours=2), "failed"), "head": "bbb222"},
        _refusal(NOW - timedelta(hours=5), "landed"),
    ]
    assert land_refusals_from_actions(rows, NOW) == [
        {"initiative": "i", "phase": "p", "run": "i-1", "attempts": 2, "last_refused_at": _stamp(NOW - timedelta(hours=2)), "head": "bbb222"}
    ]


def test_a_refusal_has_no_head_key_when_its_newest_unlanded_row_has_none():
    rows = [{**_refusal(NOW - timedelta(hours=3)), "head": "aaa111"}, _refusal(NOW - timedelta(hours=2))]
    assert land_refusals_from_actions(rows, NOW) == [
        {"initiative": "i", "phase": "p", "run": "i-1", "attempts": 2, "last_refused_at": _stamp(NOW - timedelta(hours=2))}
    ]


def test_a_refusal_head_is_read_from_the_action_json_of_a_recorded_row():
    row = {"ts": _stamp(NOW), "action_json": json.dumps({**_refusal(NOW), "head": "ccc333"})}
    [refusal] = land_refusals_from_actions([row], NOW)
    assert refusal["head"] == "ccc333"


def test_facts_carry_land_heads_for_refused_phases_only():
    calls: list[tuple[str, str]] = []

    def rev_parse(repo: str, ref: str) -> str | None:
        calls.append((repo, ref))
        return {("/r/i", "epic/i/p"): "bbb222", ("/r/i", "epic/i/q"): "ddd444"}.get((repo, ref))

    rows = [_refusal(NOW - timedelta(hours=1)), {**_refusal(NOW - timedelta(hours=2), "landed"), "phase": "q"}]
    deps = replace(_deps(), actions=lambda: rows, repos=lambda: {"i": "/r/i"}, rev_parse=rev_parse)
    facts = gather_facts(deps, NOW)
    assert [r["phase"] for r in facts["land_refusals"]] == ["p"]
    assert facts["land_heads"] == {"i/p": "bbb222"}
    assert calls == [("/r/i", "epic/i/p")]


def test_facts_carry_empty_land_heads_when_nothing_is_refused():
    deps = replace(_deps(), rev_parse=lambda repo, ref: pytest.fail("rev_parse called with no refusal"))
    assert gather_facts(deps, NOW)["land_heads"] == {}


def _store_with_recorder(tmp_path, monkeypatch, at: datetime):
    """A sqlite `chair_actions` table and the recorder wired to it as `record-action` writes it: ts, kind, target and the whole line as action_json."""
    db = tmp_path / "store.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE chair_actions (ts TEXT, kind TEXT, target TEXT, action_json TEXT)")

    def store(argv: list[str]) -> tuple[int, str]:
        line = json.loads(argv[-1])
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO chair_actions VALUES (?, ?, ?, ?)", (line["ts"], line["kind"], line.get("target", ""), json.dumps(line))
            )
        return 0, ""

    def open_store(runs_dir):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        return conn, "?"

    monkeypatch.setattr(run_store, "_open", open_store)
    return recorder(tmp_path, lambda: 4, lambda: _stamp(at), store=store, holder="h")


def _planned_kinds(tmp_path, now: datetime) -> list[str]:
    """Facts through the planner's own read path, `read_chair_actions` under `windowed_actions`, then `plan_lands`."""
    actions = windowed_actions(lambda n: read_chair_actions(tmp_path, n), now)
    # land_at level: an incomplete ticket epic plans no land, so it would leave the back-off nothing to hold off.
    facts = gather_facts(replace(_deps(), actions=actions, land_at_values=lambda: {"i": "level"}), now)
    return [a["kind"] for a in plan_lands(facts, now)]


@pytest.mark.parametrize("status", ["refused", "failed", "not_landed"])
def test_a_land_phase_refusal_the_recorder_wrote_holds_the_planner_off_until_the_back_off_elapses(tmp_path, monkeypatch, status):
    record = _store_with_recorder(tmp_path, monkeypatch, NOW)
    record({"kind": "land_phase", "status": status, "initiative": "i", "phase": "p", "run": "i-1", "reason": "x"})
    assert _planned_kinds(tmp_path, NOW + timedelta(minutes=5)) == []
    assert _planned_kinds(tmp_path, NOW + backoff_for(1)) == ["fetch", "land_phase"]


def test_two_not_landed_rows_count_two_attempts_so_the_back_off_doubles():
    rows = [_refusal(NOW - timedelta(minutes=40), "not_landed"), _refusal(NOW, "not_landed")]
    [refusal] = land_refusals_from_actions(rows, NOW)
    assert refusal["attempts"] == 2 and backoff_for(2) == 2 * backoff_for(1)


@pytest.mark.xfail(
    strict=True,
    reason="root cause outside this task's surfaces: chair_read_stale.read_chair_actions returns [] when run_store._open "
    "raises the missing-psycopg RuntimeError, while the recorder writes through the harness python, which has psycopg",
)
def test_a_reader_without_psycopg_still_holds_off_a_land_the_recorder_wrote(tmp_path, monkeypatch):
    record = _store_with_recorder(tmp_path, monkeypatch, NOW)
    record({"kind": "land_phase", "status": "refused", "initiative": "i", "phase": "p", "run": "i-1", "reason": "x"})

    def no_driver(runs_dir):
        raise RuntimeError("a postgres store needs psycopg: pip install 'coxswain[postgres]'")

    monkeypatch.setattr(run_store, "_open", no_driver)
    assert _planned_kinds(tmp_path, NOW + timedelta(minutes=5)) == []


def test_phase_records_reads_the_approved_tasks_and_is_absent_without_its_reader():
    seen: list = []
    deps = replace(_deps(), phase_records=lambda tasks: seen.append([t["id"] for t in tasks]) or ["i-1:p"])
    assert gather_facts(deps, NOW)["phase_records"] == ["i-1:p"]
    assert seen == [["a"]]
    assert "phase_records" not in gather_facts(_deps(), NOW)
