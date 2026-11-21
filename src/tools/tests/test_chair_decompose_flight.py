"""One decompose per intake in flight, whatever the worker or placement (`chair_decompose_flight`).

The incident (2026-11-21, on the chair machine): one readied intake, placed on a lane host by the launch worker. The hosted launch
returns at once, and the intake stays queued with no initiative until the run writes its rows, so each `runs`
notification the run itself sent woke the launch worker into planning `launch_decompose` again: runs -1 to -4, the last
three refused by run -1's lease. `test_the_incident_*` replays those facts. The rule is pure and tested with plain values;
the gather reads it from the action log and the decompose runs; the runs reader is tested over a real sqlite store."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from test_chair_facts import _deps as _facts_deps
from test_chair_plan_kinds import _facts

from agent_tools import run_store
from agent_tools.chair_decompose_flight import (
    QUEUED_AFTER_ROWS_CAUSE,
    RUN_MAX_S,
    SETTLE_S,
    in_flight_facts,
    in_flight_reason,
    launch_times,
    queued_after_rows,
)
from agent_tools.chair_decompose_runs import read_decompose_runs
from agent_tools.chair_decompose_streak import DecomposeRun
from agent_tools.chair_facts import gather_facts
from agent_tools.chair_plan import plan_kind
from harness.store_migrate import open_store

NOW = datetime(2026, 11, 21, 12, 35, 0, tzinfo=UTC)
PATH = "intake/2026-11-20-cartridge-rewrite-keeps-the-file-s-comments.md"
IID = "cartridge-rewrite-keeps-the-file-s-comments"


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def _run(n: int, launched_s: float, ended_s: float | None = None, wrote: bool = True) -> DecomposeRun:
    """Run `<IID>-<n>`, launched `launched_s` seconds ago, ended `ended_s` ago (None: no end yet)."""
    return DecomposeRun(
        f"{IID}-{n}", wrote_items=wrote, refusal_line=None, launched_at=_ago(launched_s),
        exited=ended_s is not None, ended_at=None if ended_s is None else _ago(ended_s),
    )


# -- the rule --------------------------------------------------------------------------------------------------------

def test_a_run_with_no_end_holds_the_intake_wherever_it_runs():
    assert in_flight_reason([_run(1, 30)], [], None, NOW) == f"its decompose run {IID}-1 has not ended"


def test_a_launch_recorded_before_its_run_reports_holds_the_intake():
    reason = in_flight_reason([], [_ago(5)], None, NOW)
    assert reason == "a decompose launch was recorded 5s ago and its run has not reported yet"
    assert in_flight_reason([], [_ago(SETTLE_S + 1)], None, NOW) is None


def test_a_run_that_just_ended_holds_the_intake_until_its_rows_can_be_read():
    assert in_flight_reason([_run(1, 200, ended_s=3, wrote=False)], [], None, NOW) == f"its decompose run {IID}-1 ended 3s ago"
    assert in_flight_reason([_run(1, 900, ended_s=SETTLE_S + 1, wrote=False)], [], None, NOW) is None  # the streak gate's now


def test_a_run_that_wrote_items_holds_the_intake_whatever_a_stale_queue_read_says():
    assert in_flight_reason([_run(1, 9000, ended_s=8000)], [], None, NOW) == f"its decompose run {IID}-1 wrote task items"


def test_a_run_with_no_end_past_the_decompose_timeout_no_longer_holds():
    assert in_flight_reason([_run(1, RUN_MAX_S + 1)], [], None, NOW) is None


def test_a_release_or_requeue_forgets_the_runs_and_launches_before_it():
    since = _ago(60)
    assert in_flight_reason([_run(1, 9000, ended_s=8000)], [_ago(120)], since, NOW) is None
    assert in_flight_reason([_run(2, 30)], [], since, NOW) == f"its decompose run {IID}-2 has not ended"


def test_launch_times_reads_only_done_decompose_launches():
    docs = [
        {"kind": "launch_decompose", "status": "done", "intake_ids": [PATH], "ts": "2026-11-21T12:34:06Z"},
        {"kind": "launch_decompose", "status": "failed", "intake_ids": [PATH], "ts": "2026-11-21T12:34:40Z"},
        {"kind": "launch_epic", "status": "done", "initiative": "x", "ts": "2026-11-21T12:34:50Z"},
        {"kind": "launch_decompose", "status": "done", "intake_ids": [PATH], "ts": "not a time"},
    ]
    assert launch_times(docs) == {PATH: [datetime(2026, 11, 21, 12, 34, 6, tzinfo=UTC)]}


def test_in_flight_facts_names_only_the_held_paths():
    facts = in_flight_facts([PATH, "intake/other.md"], {PATH: [_run(1, 30)]}, {}, {}, NOW)
    assert facts == {PATH: f"its decompose run {IID}-1 has not ended"}


# -- the incident, through the planner -------------------------------------------------------------------------------

def _decomposes(facts) -> list[dict]:
    return [a for kind in ("launch", "decompose") for a in plan_kind(kind, facts, NOW) if a["kind"] == "launch_decompose"]


def test_the_incident_without_the_fact_plans_a_second_decompose_of_a_live_run():
    # What the launch worker had at 12:34:36: the intake still queued, run -1 live on a lane host, nothing else.
    assert [a["intake_ids"] for a in _decomposes(_facts(intake=[PATH]))] == [[PATH]]


def test_the_incident_with_the_fact_plans_no_second_decompose_in_any_worker():
    held = in_flight_facts([PATH], {PATH: [_run(1, 35)]}, {PATH: [_ago(30)]}, {}, NOW)
    facts = _facts(intake=[PATH], decompose_in_flight=held)
    assert _decomposes(facts) == []
    other = "intake/another-idea.md"  # another intake still launches beside it
    assert [a["intake_ids"] for a in _decomposes({**facts, "intake": [PATH, other]})] == [[other]]


# -- the gather ------------------------------------------------------------------------------------------------------

def _launch_row(ts: str) -> dict:
    doc = {"kind": "launch_decompose", "intake_ids": [PATH], "epoch": 3, "status": "done", "ts": ts}
    return {"ts": ts, "kind": "launch_decompose", "target": PATH, "status": "done", "action_json": json.dumps(doc)}


def test_gather_holds_an_intake_whose_hosted_launch_was_recorded_before_any_run_row():
    deps = replace(
        _facts_deps(), intake=lambda: [PATH], actions=lambda: [_launch_row("2026-11-21T12:34:06Z")],
        decompose_runs=lambda path: [],
    )
    facts = gather_facts(deps, NOW)
    assert list(facts["decompose_in_flight"]) == [PATH]


def test_gather_holds_an_intake_whose_run_is_live_and_frees_it_after_a_release():
    deps = replace(_facts_deps(), intake=lambda: [PATH], actions=lambda: [], decompose_runs=lambda path: [_run(1, 120)])
    assert gather_facts(deps, NOW)["decompose_in_flight"] == {PATH: f"its decompose run {IID}-1 has not ended"}
    released = replace(deps, decompose_cutoff=lambda path: _ago(60))
    assert gather_facts(released, NOW)["decompose_in_flight"] == {}


# -- the runs reader, over a real store ------------------------------------------------------------------------------

def test_the_runs_reader_says_which_runs_have_ended_and_when(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    conn = open_store(run_store.store_url_for(runs), "2026-11-21T12:00:00Z")
    try:
        for run_id, launched, ended, status in (
            (f"{IID}-1", "2026-11-21T12:34:07+00:00", None, "running"),
            (f"{IID}-2", "2026-11-21T12:34:45+00:00", "2026-11-21T12:34:46+00:00", "refused"),
            ("other-1", "2026-11-21T12:00:00+00:00", "2026-11-21T12:10:00+00:00", "ok"),
        ):
            conn.execute(
                "INSERT INTO runs (run_id, launched_at, ended_at, status) VALUES (?, ?, ?, ?)", (run_id, launched, ended, status)
            )
    finally:
        conn.close()
    [live] = read_decompose_runs(runs, IID)  # the refused run never started, so it is not one of the intake's runs
    assert (live.run_id, live.exited, live.ended_at) == (f"{IID}-1", False, None)
    assert in_flight_reason([live], [], None, NOW) == f"its decompose run {IID}-1 has not ended"


# -- clock skew, lost runs, the told-once report ---------------------------------------------------------------------

def test_a_host_clock_ahead_of_the_chair_still_holds_a_live_run():
    ahead = DecomposeRun(f"{IID}-1", True, None, launched_at=NOW + timedelta(seconds=400), exited=False)
    assert in_flight_reason([ahead], [], None, NOW) == f"its decompose run {IID}-1 has not ended"


def test_an_empty_run_that_ended_in_the_future_reads_as_just_ended():
    run = replace(_run(1, 60, wrote=False), exited=True, ended_at=NOW + timedelta(seconds=5))
    assert in_flight_reason([run], [], None, NOW) == f"its decompose run {IID}-1 ended 0s ago"
    assert in_flight_reason([], [NOW + timedelta(seconds=5)], None, NOW) is not None  # a launch stamped ahead, too


def test_a_lost_run_no_longer_holds_the_intake():
    assert in_flight_reason([_run(1, 600)], [], None, NOW, lost={f"{IID}-1"}) is None
    assert in_flight_reason([_run(1, 600)], [], None, NOW, lost={"other-1"}) is not None


def test_a_run_that_wrote_items_is_reported_only_once_it_has_settled():
    assert queued_after_rows([_run(1, 900, ended_s=SETTLE_S + 1)], None, NOW) == f"{IID}-1"
    assert queued_after_rows([_run(1, 900, ended_s=10)], None, NOW) is None  # its rows may not be read yet
    assert queued_after_rows([_run(1, 900, ended_s=SETTLE_S + 1, wrote=False)], None, NOW) is None
    assert queued_after_rows([_run(1, 900, ended_s=SETTLE_S + 1), _run(2, 30)], None, NOW) is None  # a newer one is live
    assert queued_after_rows([_run(1, 900, ended_s=SETTLE_S + 1)], _ago(60), NOW) is None  # requeued since


def _reports(facts) -> list[dict]:
    return [a for a in plan_kind("decompose", facts, NOW) if a.get("cause") == QUEUED_AFTER_ROWS_CAUSE]


def test_the_queued_after_rows_report_is_told_once_and_marks_nothing():
    facts = _facts(intake=[PATH], decompose_queued_after_rows={PATH: f"{IID}-1"})
    [told] = _reports(facts)
    assert (told["kind"], told["intake"], told["run"]) == ("needs_chair", PATH, f"{IID}-1")
    assert _reports({**facts, "decompose_queued_after_rows_reported": [f"{PATH}|{IID}-1"]}) == []
    planned = [a for kind in ("launch", "decompose") for a in plan_kind(kind, facts, NOW) if a.get("intake") == PATH]
    assert [a["kind"] for a in planned] == ["needs_chair"]  # the report, and no write to the intake


def test_gather_reads_the_stuck_intake_and_the_reported_key():
    doc = {"kind": "needs_chair", "cause": QUEUED_AFTER_ROWS_CAUSE, "intake": PATH, "run": f"{IID}-1", "status": "recorded"}
    row = {"ts": "2026-11-21T12:00:00Z", "kind": "needs_chair", "action_json": json.dumps(doc)}
    deps = replace(
        _facts_deps(), intake=lambda: [PATH], actions=lambda: [row], decompose_runs=lambda path: [_run(1, 9000, ended_s=8000)],
    )
    facts = gather_facts(deps, NOW)
    assert facts["decompose_queued_after_rows"] == {PATH: f"{IID}-1"}
    assert facts["decompose_queued_after_rows_reported"] == [f"{PATH}|{IID}-1"]
    assert _reports(facts) == []


def test_gather_frees_an_intake_whose_live_run_is_lost():
    deps = replace(
        _facts_deps(), intake=lambda: [PATH], actions=lambda: [], decompose_runs=lambda path: [_run(1, 1200)],
        lost_runs=lambda: {IID: f"{IID}-1"},
    )
    assert gather_facts(deps, NOW)["decompose_in_flight"] == {}
