"""Under work_state store the chair reads a quarantine's attempts back from the run task records.

The board lives in the store and nothing writes an attempt into a work file: a quarantined task's exported ticket has no
`attempts:`, and its store row's `extra` holds none. The epic still saves each task's record as
`runs/<run>/tasks/<phase>/<task>.json`, with the quarantine's cause on it, and a remote lane's run comes home the same
way when the chair fetches it. These tests write those records as literal JSON and read them as the chair does.
"""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_sort, cli, run_store
from agent_tools.chair_facts import gather_facts
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_read_cache import StampCache, dump_entries
from agent_tools.chair_read_quarantined import body_sha
from agent_tools.chair_read_run_records import (
    RECORD_SOURCE,
    merged_attempts,
    read_run_records,
    read_store_attempts,
    record_rows,
    stale_attempts,
)
from agent_tools.chair_report import format_status
from agent_tools.chair_types import QUARANTINED_CAUSE, RemoteUnfetched
from tests.test_chair_facts import NOW, _deps

BODY = "\nRead the vendor schema and print its columns.\n"
WHY = "the reviewers held that the parser drops a short row"

# A code quarantine's record, in the shape a live store-mode run writes it.
CODE_RECORD = """{
  "adversary": {"objections": []},
  "arbitration": {"verdict": "approve", "reasoning": "the change does what the ticket asks"},
  "build": {"patch": "--- a/probe.py\\n+++ b/probe.py\\n", "files_touched": ["probe.py"]},
  "cause": "code",
  "cause_why": "rule: kind unverified",
  "change_facts": {"checks": [{"name": "pytest", "cmd": "pytest -q", "passed": false, "exit_code": 1,
                               "output_tail": "FAILED tests/test_probe.py::test_short_row\\n1 failed, 11 passed"}]},
  "date": "%(date)s",
  "fix_loop": {"attempts": 1, "stopped": null},
  "handoff": null,
  "initiative": "demo",
  "phase": "p1",
  "plan": {"steps": []},
  "plan_attack": null,
  "plan_competition": null,
  "plan_gate": null,
  "proposals": [{"kind": "draft_pr_create", "target": "t1"}],
  "review": {"verdict": "approve", "findings": []},
  "review_tier": "standard",
  "run_id": "%(run)s:p1",
  "scope": {"state": "ready"},
  "ticket": "%(task)s"
}"""
REVIEW_RECORD = """{
  "adversary": {"objections": [{"claim": "the parser drops a short row"}]},
  "arbitration": {"verdict": "revise", "reasoning": "the parser drops a short row without a test"},
  "build": {"patch": ""},
  "cause": "review",
  "cause_why": "%(why)s",
  "date": "%(date)s",
  "fix_loop": {"attempts": 3, "stopped": "no change"},
  "initiative": "demo",
  "phase": "p1",
  "proposals": [],
  "review": {"verdict": "revise"},
  "run_id": "%(run)s:p1",
  "ticket": "%(task)s"
}"""


def _record(runs: Path, run: str, task: str, text: str, *, date: str = "2026-11-12", **fields: object) -> Path:
    path = runs / run / "tasks" / "p1" / f"{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.loads(text % {"run": run, "task": task, "date": date, "why": WHY})
    path.write_text(json.dumps({**record, **fields}), encoding="utf-8")
    return path


def _row(task: str, state: str = "quarantined", body: str = BODY) -> dict:
    """A queue row as the store holds it: `extra` carries id, phase and priority, and no attempts."""
    return {
        "kind": "task", "initiative": "demo", "phase": "p1", "task_id": task, "state": state, "needs": [],
        "holder": None, "expires_at": None, "body": body, "title": task,
        "extra": {"id": task, "phase": "p1", "priority": 2},
    }


def _facts(
    ws: Path, rows: list[dict], hold_after: object = None, *, remote_unfetched=None, handled=(), has_patch: bool = False,
):
    deps = replace(
        _deps(has_patch=has_patch), queue=lambda: rows, quarantined=lambda: (), stranded=lambda: (),
        attempts=lambda: read_store_attempts(ws, {}, rows), quarantine_hold_after=lambda: hold_after,
        remote_unfetched=None if remote_unfetched is None else (lambda: remote_unfetched), handled_keys=lambda: list(handled),
    )
    return gather_facts(deps, NOW)


def _quarantine(facts, task: str) -> dict:
    return next(q for q in facts["quarantines"] if q["task_id"] == task)


# ── the facts ──────────────────────────────────────────────────────────────


def test_a_quarantined_store_row_with_a_record_reads_its_cause_run_and_reason(tmp_path):
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD)
    q = _quarantine(_facts(tmp_path, [_row("t1")]), "t1")
    assert (q["cause"], q["run"], q["causes"], q["failures"]) == ("code", "demo-3", ["code"], 1)
    assert "check pytest failed (exit 1): 1 failed, 11 passed" in q["reason"]
    assert q["held_objection"] == q["reason"]


def test_a_quarantined_store_row_with_no_record_stays_quarantined_with_no_run(tmp_path):
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD)
    q = _quarantine(_facts(tmp_path, [_row("t1"), _row("t2")]), "t2")
    assert (q["cause"], q["run"], q["reason"], q["failures"]) == (QUARANTINED_CAUSE, "", "", 0)


def test_the_chair_asks_triage_for_the_recorded_quarantine_and_the_chair_for_the_unrecorded_one(tmp_path):
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD)
    plan = plan_recover(_facts(tmp_path, [_row("t1"), _row("t2")]))
    mine = [(a["kind"], a.get("task_id"), a.get("key")) for a in plan if a.get("task_id") in ("t1", "t2")]
    assert mine == [("request_triage", "t1", chair_sort.triage_key("t1", "demo-3")), ("needs_chair", "t2", None)]


def test_the_newest_record_wins(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "demo-2", "t1", CODE_RECORD, date="2026-11-10")
    _record(runs, "demo-9", "t1", REVIEW_RECORD, date="2026-11-12")
    _record(runs, "demo-10", "t1", CODE_RECORD, date="2026-11-12")  # the same day as demo-9: the run number decides
    q = _quarantine(_facts(tmp_path, [_row("t1")]), "t1")
    assert (q["cause"], q["run"], q["causes"]) == ("code", "demo-10", ["code", "review", "code"])
    assert q["reasons"][1].startswith(WHY) and "the fix loop stopped: no change" in q["reasons"][1]


def test_a_record_built_on_another_ticket_body_is_not_on_the_current_body(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "demo-2", "t1", REVIEW_RECORD, date="2026-11-10", ticket_body=BODY)
    _record(runs, "demo-3", "t1", CODE_RECORD, ticket_body="an older body")
    _record(runs, "demo-4", "t2", CODE_RECORD)  # no body on the record: it cannot be told apart, so it counts
    rows = read_store_attempts(tmp_path, {}, [_row("t1"), _row("t2")])
    assert [(r["task"], r["run"], r["on_current_body"]) for r in rows] == [
        ("t1", "demo-2", True), ("t1", "demo-3", False), ("t2", "demo-4", True),
    ]
    assert rows[1]["body_sha"] == body_sha("an older body") and "body_sha" not in rows[2]
    q = _quarantine(_facts(tmp_path, [_row("t1")]), "t1")
    # Only the attempt on the current body names the state; the count covers every body.
    assert (q["run"], q["causes"], q["failures"]) == ("demo-2", ["review"], 2)


def test_the_hold_count_reads_the_records(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "demo-1", "t1", CODE_RECORD, date="2026-11-11")
    # A stoppage is not the task's failure: a harness cause never counts toward the hold.
    _record(runs, "demo-2", "t1", CODE_RECORD, date="2026-11-12", cause="harness", cause_why="rule: kind infra")
    _record(runs, "demo-3", "t1", REVIEW_RECORD, date="2026-11-13")
    _record(runs, "demo-4", "t1", CODE_RECORD, date="2026-11-14")
    facts = _facts(tmp_path, [_row("t1")], hold_after=3)
    assert _quarantine(facts, "t1")["failures"] == 3
    plan = [a for a in plan_recover(facts) if a.get("task_id") == "t1"]
    assert [(a["kind"], a["key"]) for a in plan] == [("hold_task", chair_sort.hold_key("t1", "demo-4"))]
    plan = [a for a in plan_recover(_facts(tmp_path, [_row("t1")], hold_after=4)) if a.get("task_id") == "t1"]
    assert [(a["kind"], a["key"]) for a in plan] == [("request_triage", chair_sort.triage_key("t1", "demo-4"))]


def test_a_quarantine_waits_for_the_fetch_of_its_initiative_newest_remote_run(tmp_path):
    # demo-3 was triaged already; demo-4 ran on a lane host and quarantined the task again. Its record comes home with
    # the fetch this tick plans: acting on demo-3's record now would hold the task as though triage had left it as it was.
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD)
    rows = [_row("t1")]
    deps = replace(
        _deps(), queue=lambda: rows, quarantined=lambda: (), stranded=lambda: (), remote_unfetched=lambda: {"demo": "demo-4"},
        attempts=lambda: read_store_attempts(tmp_path, {}, rows), handled_keys=lambda: [chair_sort.triage_key("t1", "demo-3")],
    )
    facts = gather_facts(deps, NOW)
    assert not [q for q in facts["quarantines"] if q["task_id"] == "t1"]
    assert not [a for a in plan_recover(facts) if a.get("task_id") == "t1"]
    # It is not hidden: the facts name it, and the status line says what it waits for.
    assert facts["awaiting_fetch"] == [{"initiative": "demo", "task_id": "t1", "run": "demo-4"}]
    assert "awaiting fetch: demo:t1 (run demo-4)" in format_status(facts, [], [], NOW).split(" | ")
    # A decompose-only unfetched run brings home no task record, so it holds nothing back.
    decompose = RemoteUnfetched({"demo": "demo-4"}, ["demo"])
    facts = gather_facts(replace(deps, remote_unfetched=lambda: decompose), NOW)
    assert [(a["kind"], a["key"]) for a in plan_recover(facts) if a.get("task_id") == "t1"] == [
        ("hold_task", chair_sort.hold_key("t1", "demo-3")),
    ]
    assert facts["awaiting_fetch"] == []


def test_an_older_unfetched_remote_run_never_holds_back_a_newer_quarantine(tmp_path):
    # demo-4 ran on a lane host that is gone and is never fetched; demo-5 ran here and quarantined the task since.
    _record(tmp_path / "runs", "demo-5", "t1", CODE_RECORD)
    facts = _facts(tmp_path, [_row("t1")], remote_unfetched=RemoteUnfetched({"demo": "demo-4"}, []))
    assert (_quarantine(facts, "t1")["run"], facts["awaiting_fetch"]) == ("demo-5", [])
    assert [(a["kind"], a["key"]) for a in plan_recover(facts) if a.get("task_id") == "t1"] == [
        ("request_triage", chair_sort.triage_key("t1", "demo-5")),
    ]


def test_a_quarantine_with_no_record_waits_for_an_unfetched_run_and_says_so(tmp_path):
    # The unfetched run may be the one that quarantined it: its record is the only one there will be.
    facts = _facts(tmp_path, [_row("t1")], remote_unfetched=RemoteUnfetched({"demo": "demo-4"}, []))
    assert not [q for q in facts["quarantines"] if q["task_id"] == "t1"]
    assert facts["awaiting_fetch"] == [{"initiative": "demo", "task_id": "t1", "run": "demo-4"}]


def test_the_files_path_reports_no_quarantine_awaiting_a_fetch():
    facts = gather_facts(replace(_deps(), remote_unfetched=lambda: {"i": "i-9"}), NOW)
    assert facts["awaiting_fetch"] == [] and "a" in [q["task_id"] for q in facts["quarantines"]]


# ── the newest run ─────────────────────────────────────────────────────────


def test_the_newest_run_wins_whatever_its_record_is_dated(tmp_path):
    runs = tmp_path / "runs"
    # demo-9 failed on an invoke, so the driver wrote the minimal record, which has no date: its file times it, later in
    # the day than demo-10's dated record. One initiative's runs never overlap, so the run number decides.
    minimal = runs / "demo-9" / "tasks" / "p1" / "t1.json"
    minimal.parent.mkdir(parents=True)
    minimal.write_text(json.dumps({"ticket": "t1", "initiative": "demo", "phase": "p1", "cause": "harness", "cause_why": "rule: kind infra"}))
    late = datetime(2026, 11, 12, 23, 0, tzinfo=UTC).timestamp()
    os.utime(minimal, (late, late))
    _record(runs, "demo-10", "t1", CODE_RECORD, date="2026-11-12")
    facts = _facts(tmp_path, [_row("t1")])
    assert (_quarantine(facts, "t1")["run"], _quarantine(facts, "t1")["cause"]) == ("demo-10", "code")
    assert [(a["kind"], a["key"]) for a in plan_recover(facts) if a.get("task_id") == "t1"] == [
        ("request_triage", chair_sort.triage_key("t1", "demo-10")),
    ]


def test_merged_attempts_are_ordered_by_run_then_time():
    file_rows = [{"initiative": "demo", "phase": "p1", "task": "t1", "run": "demo-4", "cause": "code", "ts": "2026-11-13T01:00:00Z"}]
    records = [{"initiative": "demo", "phase": "p1", "task": "t1", "run": "demo-5", "cause": "review", "ts": "2026-11-13"}]
    assert [r["run"] for r in merged_attempts(file_rows, records)] == ["demo-4", "demo-5"]


# ── a rescue ───────────────────────────────────────────────────────────────


def test_a_kept_patch_is_rescued_once_for_the_run_that_quarantined_it(tmp_path):
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD, cause="harness", cause_why="rule: kind infra")
    plan = [a for a in plan_recover(_facts(tmp_path, [_row("t1")], has_patch=True)) if a.get("task_id") == "t1"]
    assert [(a["kind"], a.get("key")) for a in plan] == [("rescue", chair_sort.rescue_key("t1", "demo-3"))]
    handled = [chair_sort.rescue_key("t1", "demo-3")]
    plan = [a for a in plan_recover(_facts(tmp_path, [_row("t1")], has_patch=True, handled=handled)) if a.get("task_id") == "t1"]
    assert [a["kind"] for a in plan] == ["needs_chair"]


# ── the stale count ────────────────────────────────────────────────────────


def _stale(tmp_path: Path, monkeypatch, body: str) -> dict:
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: [_row("t1", body=body)])
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None, cache=None: [])
    # The workspace is out of git, as on the machine this was found on: the board's files are the store's rows rendered.
    profile = {"chair": {"workspace_git": False}}
    deps = cli._chair_run_deps(tmp_path / "runs", profile, "chair", 1, "h", True, print, tmp_path / "profile.yaml", "store")
    (candidate,) = deps.facts_deps.stale_candidates(NOW)  # type: ignore[misc]
    return candidate


def test_a_record_that_names_no_body_is_left_out_of_the_stale_count(tmp_path, monkeypatch):
    # Two failures, triage amended the ticket in between: the count is of the current body, which no record can name.
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD, date="2026-11-10")
    _record(tmp_path / "runs", "demo-4", "t1", CODE_RECORD, date="2026-11-12")
    amended = BODY + "\nTriage: add a test for the short row.\n"
    assert _stale(tmp_path, monkeypatch, amended)["quarantine_non_harness_count"] == 0


def test_a_record_stamped_with_its_body_counts_only_on_that_body(tmp_path, monkeypatch):
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD, date="2026-11-10", body_sha=body_sha(BODY))
    _record(tmp_path / "runs", "demo-4", "t1", CODE_RECORD, date="2026-11-12", body_sha=body_sha(BODY))
    assert _stale(tmp_path, monkeypatch, BODY)["quarantine_non_harness_count"] == 2
    amended = BODY + "\nTriage: add a test for the short row.\n"
    assert _stale(tmp_path, monkeypatch, amended)["quarantine_non_harness_count"] == 0
    rows = read_store_attempts(tmp_path, {}, [_row("t1", body=amended)])
    assert [r["on_current_body"] for r in rows] == [False, False]


def test_the_stale_count_keeps_work_file_attempts_and_stamped_records():
    rows = [
        {"run": "demo-1", "path": "work/demo/p1/t1.md"},
        {"run": "demo-2", "source": RECORD_SOURCE, "body_sha": "abcdef012345"},
        {"run": "demo-3", "source": RECORD_SOURCE},
    ]
    assert [r["run"] for r in stale_attempts(rows)] == ["demo-1", "demo-2"]


# ── the cache ──────────────────────────────────────────────────────────────


def test_the_cache_keeps_what_the_chair_reads_of_a_record_not_the_record(tmp_path):
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD)
    cache = StampCache(8)
    ((_, _, _, kept, _),) = read_run_records(tmp_path / "runs", {("p1", "t1")}, cache)
    (entry,) = dump_entries(cache)["entries"]
    assert entry["value"]["attempt"] == kept
    assert set(kept) == {"cause", "cause_why", "reason", "kind", "initiative", "date"}
    assert read_run_records(tmp_path / "runs", {("p1", "t1")}, cache)[0][3] == kept and cache.reads == (1, 1)


# ── the reader ─────────────────────────────────────────────────────────────


def test_only_an_open_task_with_a_record_that_names_a_cause_gets_a_row(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "demo-3", "t1", CODE_RECORD)
    _record(runs, "demo-3", "t2", CODE_RECORD, cause=None)  # an approved build names no cause
    _record(runs, "demo-3", "t3", CODE_RECORD)  # done: no longer open
    _record(runs, "demo-3", "t4", CODE_RECORD)  # ready again after triage was asked for: still open
    _record(runs, "other-1", "t1", CODE_RECORD, initiative="other")  # another initiative's task of the same id
    (runs / "demo-3" / "tasks" / "p1" / "t5.json").write_text("{not json", encoding="utf-8")
    rows = [_row("t1"), _row("t2"), _row("t3", "done"), _row("t4", "ready"), _row("t5")]
    got = read_store_attempts(tmp_path, {}, rows)
    assert [(r["initiative"], r["task"], r["run"], r["cause"], r["kind"], r["ts"]) for r in got] == [
        ("demo", "t1", "demo-3", "code", "unverified", "2026-11-12"),
        ("demo", "t4", "demo-3", "code", "unverified", "2026-11-12"),
    ]
    assert got[0]["path"] == "runs/demo-3/tasks/p1/t1.json"


def test_a_failed_rescue_reads_as_the_files_path_reads_it(tmp_path):
    _record(tmp_path / "runs", "demo-5", "t1", CODE_RECORD, cause_why="rule: rescue checks failed")
    (row,) = read_store_attempts(tmp_path, {}, [_row("t1")])
    assert (row["kind"], row["cause"]) == ("rescue_failed", "rescue_failed")


def test_a_record_with_no_date_is_timed_by_its_file(tmp_path):
    path = _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD, date="")
    ((run, phase, task, record, mtime),) = read_run_records(tmp_path / "runs", {("p1", "t1")})
    assert (run, phase, task, record["cause"]) == ("demo-3", "p1", "t1", "code")
    assert mtime == datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    (row,) = record_rows([(run, phase, task, record, mtime)], [_row("t1")])
    assert row["ts"] == mtime


def test_the_reader_opens_only_the_records_of_the_tasks_it_was_asked_for(tmp_path):
    runs = tmp_path / "runs"
    _record(runs, "demo-3", "t1", CODE_RECORD)
    _record(runs, "demo-3", "t2", CODE_RECORD)
    assert [r[2] for r in read_run_records(runs, {("p1", "t2")})] == ["t2"]
    assert read_run_records(tmp_path / "absent", {("p1", "t2")}) == []


def test_a_run_the_work_file_already_names_is_not_counted_twice():
    file_rows = [{"initiative": "demo", "phase": "p1", "task": "t1", "run": "demo-3", "cause": "code", "ts": "2026-11-12T01:00:00Z"}]
    records = [
        {"initiative": "demo", "phase": "p1", "task": "t1", "run": "demo-3", "cause": "code", "ts": "2026-11-12"},
        {"initiative": "demo", "phase": "p1", "task": "t1", "run": "demo-4", "cause": "review", "ts": "2026-11-13"},
    ]
    assert [(r["run"], r["ts"]) for r in merged_attempts(file_rows, records)] == [
        ("demo-3", "2026-11-12T01:00:00Z"), ("demo-4", "2026-11-13"),
    ]


# ── the wiring ─────────────────────────────────────────────────────────────


def _chair_attempts(tmp_path: Path, monkeypatch, mode: str) -> list[dict]:
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: [_row("t1")])
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None, cache=None: [])
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", True, print, tmp_path / "profile.yaml", mode)
    return list(deps.facts_deps.attempts())


def test_the_chair_reads_the_records_under_work_state_store(tmp_path, monkeypatch):
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD)
    assert [(r["task"], r["run"], r["cause"]) for r in _chair_attempts(tmp_path, monkeypatch, "store")] == [("t1", "demo-3", "code")]


def test_the_chair_reads_no_record_under_work_state_files(tmp_path, monkeypatch):
    _record(tmp_path / "runs", "demo-3", "t1", CODE_RECORD)
    assert _chair_attempts(tmp_path, monkeypatch, "files") == []
