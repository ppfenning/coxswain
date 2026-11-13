"""The chair handles a quarantine on its own: a stoppage is retried, a failure that needs a tweak goes to the model's
triage first, and a task that has failed too often is held for a person with a visible mark."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_tools import chair_exec, chair_sort, dash_feed_tasks, route
from agent_tools.chair_exec import Deps, perform
from agent_tools.chair_facts import (
    DEFAULT_QUARANTINE_HOLD_AFTER,
    gather_facts,
    held_tasks_from_rows,
    quarantine_facts,
    resolve_quarantine_hold_after,
)
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_read_quarantined import body_sha, quarantined_from_rows
from agent_tools.chair_report import needs_chair_items
from core.workstore import HOLD_MARK, HOLD_REASON, TRIAGE_FLAG
from tests.test_chair_facts import NOW, _deps, _row

OBJECTION = "the parser drops a short row without a test"
BODY = "\nBody.\n"


# ── facts ──────────────────────────────────────────────────────────────────


def test_a_quarantined_store_row_carries_the_reason_of_its_newest_attempt_on_the_body():
    attempts = [
        {"run": "i-1", "ts": "2026-09-25T01:00:00Z", "cause": "review", "reason": "first", "body_sha": body_sha(BODY)},
        {"run": "i-2", "ts": "2026-09-26T01:00:00Z", "cause": "review", "reason": "second", "body_sha": body_sha(BODY)},
        {"run": "i-3", "ts": "2026-09-27T01:00:00Z", "cause": "review", "reason": "older body", "body_sha": "0" * 12},
    ]
    row = {"kind": "task", "initiative": "i", "task_id": "t1", "phase": "p1", "state": "quarantined", "body": BODY,
           "extra": {"attempts": attempts}}
    assert quarantined_from_rows([row])[0]["reason"] == "second"


def _fact(*attempts, run="i-4", reason=OBJECTION):
    opened = [{"initiative": "i", "phase": "p1", "task": "a", "run": run, "reason": reason}]
    rows = [{"initiative": "i", "phase": "p1", "task": "a", **a} for a in attempts]
    (q,) = quarantine_facts(opened, [], rows, set(), lambda i, t: False, approved=[])
    return q


def test_failures_count_the_tasks_own_failures_across_every_body_and_no_stoppage():
    q = _fact(
        {"cause": "code", "on_current_body": False},
        {"cause": "harness"},
        {"cause": "rescue_failed"},
        {"cause": "review"},
        {"cause": None},
    )
    assert q["failures"] == 3
    assert q["causes"] == ["harness", "rescue_failed", "review", ""]


def test_a_blocked_row_with_the_mark_is_a_held_task_and_any_other_row_is_not():
    rows = [
        {"kind": "task", "initiative": "i", "task_id": "t1", "phase": "p1", "state": "blocked",
         "extra": {HOLD_REASON: f"{HOLD_MARK} pick the schema"}},
        {"kind": "task", "initiative": "i", "task_id": "t2", "phase": "p1", "state": "blocked", "extra": {}},
        {"kind": "task", "initiative": "i", "task_id": "t3", "phase": "p1", "state": "ready",
         "extra": {HOLD_REASON: f"{HOLD_MARK} stale"}},
        {"kind": "initiative", "initiative": "i", "task_id": "i", "state": "blocked", "extra": {HOLD_REASON: f"{HOLD_MARK} x"}},
    ]
    assert held_tasks_from_rows(rows) == [
        {"initiative": "i", "task_id": "t1", "phase": "p1", "reason": f"{HOLD_MARK} pick the schema"},
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 3), (0, 0), (0.0, 0), (5, 5), (5.0, 5), (-1, 3), (2.5, 3), ("4", 3), (True, 3)],
)
def test_the_hold_limit_reads_the_profile_value_and_falls_back_to_three(raw, expected):
    assert DEFAULT_QUARANTINE_HOLD_AFTER == 3
    assert resolve_quarantine_hold_after(raw) == expected


def test_the_facts_carry_the_held_tasks_and_the_hold_limit():
    rows = [_row("i", "t1", "blocked", extra={HOLD_REASON: f"{HOLD_MARK} pick one"})]
    facts = gather_facts(replace(_deps(), queue=lambda: rows, quarantine_hold_after=lambda: 0.0), NOW)
    assert facts["held_tasks"] == [{"initiative": "i", "task_id": "t1", "phase": "p1", "reason": f"{HOLD_MARK} pick one"}]
    assert facts["quarantine_hold_after"] == 0
    assert gather_facts(_deps(), NOW)["quarantine_hold_after"] == 3


def test_the_profile_takes_the_hold_limit_under_chair():
    profile, _ = route._parse_profile("chair:\n  quarantine_hold_after: 0\n")
    assert profile["chair"]["quarantine_hold_after"] == 0


# ── the plan ───────────────────────────────────────────────────────────────


def _quarantine(**over) -> dict:
    base = {
        "task_id": "t1", "initiative": "i", "cause": "review", "harness_failures": 0, "has_patch": False,
        "rescue_failed": False, "causes": ["review"], "reasons": [OBJECTION], "run": "i-4", "reason": OBJECTION,
        "held_objection": OBJECTION, "failures": 1,
    }
    return {**base, **over}


def _facts(*quarantines, handled=(), held=(), hold_after=None, initiatives=()) -> dict:
    facts = {
        "approved": [], "initiatives": list(initiatives), "quarantines": list(quarantines), "schema_deaths": {},
        "landing": [], "handled_keys": list(handled), "held_tasks": list(held),
    }
    return {**facts, **({} if hold_after is None else {"quarantine_hold_after": hold_after})}


@pytest.mark.parametrize("cause", ["review", "code", "ticket", "unknown", "quarantined"])
def test_a_failure_that_needs_a_tweak_requests_triage_once_per_state(cause):
    plan = plan_recover(_facts(_quarantine(cause=cause, causes=[cause])))  # type: ignore[arg-type]
    assert [(a["kind"], a["task_id"], a["key"]) for a in plan] == [("request_triage", "t1", chair_sort.triage_key("t1", "i-4"))]
    assert not any(a["kind"] in ("annotate_objection", "needs_chair", "relaunch") for a in plan)


def test_a_first_failed_check_requests_triage_rather_than_a_plain_relaunch():
    initiative = {"id": "i", "started": True, "ready_tasks": [{"id": "t2", "needs": []}], "landed": set()}
    plan = plan_recover(_facts(_quarantine(cause="code", causes=["code"]), initiatives=[initiative]))  # type: ignore[arg-type]
    # The request does not hold the initiative back: its other ready work relaunches after it, and picks the task up.
    assert [a["kind"] for a in plan] == ["request_triage", "clear_branches", "relaunch"]


def test_a_triage_already_requested_for_this_state_holds_the_task_for_a_person():
    plan = plan_recover(_facts(_quarantine(), handled=[chair_sort.triage_key("t1", "i-4")]))  # type: ignore[arg-type]
    assert [(a["kind"], a["key"]) for a in plan] == [("hold_task", chair_sort.hold_key("t1", "i-4"))]
    assert plan[0]["reason"].startswith(HOLD_MARK) and "triage" in plan[0]["reason"]


def test_at_the_hold_limit_the_task_is_held_with_the_mark_and_not_triaged():
    plan = plan_recover(_facts(_quarantine(failures=3)))  # type: ignore[arg-type]
    assert [a["kind"] for a in plan] == ["hold_task"]
    assert plan[0]["reason"].startswith(HOLD_MARK)
    assert "3 times" in plan[0]["reason"] and OBJECTION in plan[0]["reason"]


def test_the_hold_limit_is_a_setting_and_zero_turns_it_off():
    assert [a["kind"] for a in plan_recover(_facts(_quarantine(failures=2), hold_after=2))] == ["hold_task"]  # type: ignore[arg-type]
    assert [a["kind"] for a in plan_recover(_facts(_quarantine(failures=9), hold_after=0))] == ["request_triage"]  # type: ignore[arg-type]


def test_a_hold_already_made_for_this_state_reaches_the_chair_instead_of_holding_again():
    plan = plan_recover(_facts(_quarantine(failures=3), handled=[chair_sort.hold_key("t1", "i-4")]))  # type: ignore[arg-type]
    assert [a["kind"] for a in plan] == ["needs_chair"]


def test_a_stoppage_is_still_retried_plainly():
    stop = _quarantine(cause="harness", causes=["harness"], harness_failures=1, failures=0, task_state_key="i-4")
    del stop["held_objection"]
    plan = plan_recover(_facts(stop))  # type: ignore[arg-type]
    assert [a["kind"] for a in plan] == ["rebuild_quarantined"]


def test_a_tweak_quarantine_whose_fact_names_no_state_reaches_the_chair_as_before():
    old = {"task_id": "t1", "initiative": "i", "cause": "review", "harness_failures": 0, "has_patch": False, "rescue_failed": False}
    assert [a["kind"] for a in plan_recover(_facts(old))] == ["needs_chair"]  # type: ignore[arg-type]
    # A store quarantine with no attempt names no run: triage would have no record to cite.
    assert [a["kind"] for a in plan_recover(_facts(_quarantine(run="")))] == ["needs_chair"]  # type: ignore[arg-type]


HELD = {"initiative": "i", "task_id": "t3", "phase": "p1", "reason": f"{HOLD_MARK} triage says pick a schema"}


def test_a_held_task_is_reported_every_tick_and_announced_once():
    plan = plan_recover(_facts(held=[HELD]))  # type: ignore[arg-type]
    assert [(a["kind"], a.get("cause")) for a in plan] == [("needs_chair", "held t3"), ("announce_hold", None)]
    assert plan[0]["reason"] == HELD["reason"] and plan[1]["reason"] == HELD["reason"]
    again = plan_recover(_facts(held=[HELD], handled=[plan[1]["key"]]))  # type: ignore[arg-type]
    assert [a["kind"] for a in again] == ["needs_chair"]
    assert needs_chair_items(again) == ["needs chair: i:held t3"]


def test_a_new_hold_reason_on_the_same_task_is_announced_again():
    first = plan_recover(_facts(held=[HELD]))[1]["key"]  # type: ignore[arg-type]
    other = plan_recover(_facts(held=[{**HELD, "reason": f"{HOLD_MARK} another question"}]))[1]["key"]  # type: ignore[arg-type]
    assert first != other


# ── the executors ──────────────────────────────────────────────────────────


class FakeStore:
    """One task row; `upserts` is every row written to it, `states` every set_state call, in order."""

    def __init__(self, state: str, extra: dict | None = None) -> None:
        self.row = {"initiative": "i", "task_id": "t1", "state": state, "extra": extra or {}, "body": BODY}
        self.writes: list = []
        self.result = chair_exec.store_cli.StateSet({})

    def read_queue(self, runs_dir, initiative=None, kind=None):
        return [dict(self.row)]

    def upsert_row_detail(self, runs_dir, row) -> str:
        self.writes.append(("upsert", row))
        self.row = {**self.row, **{k: v for k, v in row.items() if k not in ("initiative", "task_id")}}
        return ""

    def set_state(self, *args, **kwargs):
        self.writes.append(("state", args))
        return self.result


def _perform(tmp_path: Path, monkeypatch, store: FakeStore, action: dict, handled: list) -> list:
    monkeypatch.setattr(chair_exec.run_store, "read_queue", store.read_queue)
    monkeypatch.setattr(chair_exec.run_store, "upsert_row_detail", store.upsert_row_detail)
    monkeypatch.setattr(chair_exec.store_cli, "set_state", store.set_state)
    deps = Deps(
        run=lambda argv: (0, ""), delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=lambda action: None, run_id=lambda action: "", repo_for=lambda action: "r",
        runs_dir=tmp_path / "runs", work_dir=tmp_path, mark_handled=handled.append,
    )
    return perform([{**action, "epoch": 1}], deps, lambda: 1, False)  # type: ignore[list-item]


REQUEST = {"kind": "request_triage", "initiative": "i", "task_id": "t1", "key": "triage:t1:i-4", "cause": "review", "reason": OBJECTION}


def test_a_triage_request_flags_the_row_then_sets_it_ready_and_marks_its_key(tmp_path, monkeypatch):
    store, handled = FakeStore("quarantined", {"keep": 1}), []
    [result] = _perform(tmp_path, monkeypatch, store, REQUEST, handled)
    assert result["status"] == "done"
    assert store.writes == [
        ("upsert", {"initiative": "i", "task_id": "t1", "extra": {"keep": 1, TRIAGE_FLAG: "triage:t1:i-4"}}),
        ("state", (tmp_path / "runs", "i", "t1", "ready", "chair-loop", "quarantined")),
    ]
    assert handled == ["triage:t1:i-4"]


def test_a_triage_request_for_a_task_no_longer_quarantined_writes_nothing(tmp_path, monkeypatch):
    store, handled = FakeStore("ready"), []
    [result] = _perform(tmp_path, monkeypatch, store, REQUEST, handled)
    assert (result["status"], store.writes, handled) == ("skipped", [], [])


def test_a_refused_triage_request_puts_the_row_back_and_leaves_the_key_open(tmp_path, monkeypatch):
    store, handled = FakeStore("quarantined", {"keep": 1}), []
    store.result = chair_exec.store_cli.StateRefused("state is ready", "ready")
    [result] = _perform(tmp_path, monkeypatch, store, REQUEST, handled)
    assert result["status"] in ("skipped", "failed")
    assert store.row["extra"] == {"keep": 1} and handled == []


HOLD = {"kind": "hold_task", "initiative": "i", "task_id": "t1", "key": "hold:t1:i-4", "cause": "review",
        "reason": f"{HOLD_MARK} failed 3 times"}


def test_a_hold_writes_the_reason_first_then_blocks_the_task_and_marks_its_key(tmp_path, monkeypatch):
    store, handled = FakeStore("quarantined", {TRIAGE_FLAG: "old"}), []
    [result] = _perform(tmp_path, monkeypatch, store, HOLD, handled)
    assert result["status"] == "done"
    assert store.writes == [
        ("upsert", {"initiative": "i", "task_id": "t1", "extra": {HOLD_REASON: f"{HOLD_MARK} failed 3 times"}}),
        ("state", (tmp_path / "runs", "i", "t1", "blocked", "chair-loop", "quarantined")),
    ]
    assert handled == ["hold:t1:i-4"]


def test_an_announcement_writes_one_courier_entry_and_marks_its_key(tmp_path, monkeypatch):
    handled: list = []
    action = {"kind": "announce_hold", "initiative": "i", "task_id": "t3", "key": "announced:k", "reason": HELD["reason"]}
    [result] = _perform(tmp_path, monkeypatch, FakeStore("blocked"), action, handled)
    entries = [json.loads(line) for line in (tmp_path / "courier.jsonl").read_text(encoding="utf-8").splitlines()]
    assert result["status"] == "done" and handled == ["announced:k"]
    assert len(entries) == 1 and entries[0]["event"] == "needs_attention"
    assert HELD["reason"] in entries[0]["note"] and entries[0]["ref"] == "coxswain://task/t3"


# ── through the whole tick plan ────────────────────────────────────────────


def test_a_tick_plans_the_triage_request_the_hold_and_the_report_through_plan_tick():
    from agent_tools.chair_plan import plan_tick
    from tests.test_chair_sort_wire import NOW as TICK_NOW
    from tests.test_chair_sort_wire import _tick_facts

    facts = {**_tick_facts(), "quarantines": [_quarantine(), _quarantine(task_id="t2", failures=3)], "held_tasks": [HELD]}
    kinds = [(a["kind"], a.get("task_id")) for a in plan_tick(facts, TICK_NOW)]  # type: ignore[arg-type]
    assert ("request_triage", "t1") in kinds and ("hold_task", "t2") in kinds
    assert ("needs_chair", "t3") in kinds and ("announce_hold", "t3") in kinds


# ── the dash feed ──────────────────────────────────────────────────────────


def test_the_dash_feed_shows_a_held_task_blocked_with_its_reason():
    assert dash_feed_tasks.HOLD_REASON == HOLD_REASON
    rows = [{"task_id": "t3", "phase": "p1", "state": "blocked", "extra": {HOLD_REASON: HELD["reason"]}}]
    [entry] = dash_feed_tasks.task_entries(rows, {}, {}, {})
    assert (entry["state"], entry["reason"]) == ("blocked", HELD["reason"])
