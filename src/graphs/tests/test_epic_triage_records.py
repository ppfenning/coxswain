"""Under work_state store a task the chair flagged for triage reaches it with the attempts its run task records hold.

The board lives in the store and nothing writes an attempt into the work file, so the item the epic reads carries no
`attempts`. Triage must cite the record, and with no attempt it had nothing to cite and refused every answer. The
earlier runs' task records (`runs/<run>/tasks/<phase>/<task>.json`, and the store's copy another lane wrote) are the
record, so each one that names a cause is handed to triage as an attempt.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.workstore import HOLD_MARK, HOLD_REASON, TRIAGE_FLAG, TRIAGE_RECORDS
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)
from tests.test_epic_triage_classes import (
    DIAGNOSIS,
    OBJECTION,
    TRIAGE_SPECS,
    TriageClassRunner,
    _answer,
    _built,
    _row,
    _store,
    _triage_calls,
)

PHASE = "p1-foundations"
# A review quarantine's record, in the shape a store-mode run writes it.
RECORD = json.loads("""{
  "adversary": {"objections": [{"claim": "the probe prints no column"}]},
  "arbitration": {"verdict": "revise", "reasoning": "the probe reads nothing"},
  "build": {"patch": "", "evidence": [{"check": "pytest", "output": "1 passed"}]},
  "cause": "review",
  "cause_why": "the reviewers held that the probe reads nothing",
  "date": "2026-08-30",
  "fix_loop": {"attempts": 3, "stopped": "no change"},
  "initiative": "demo-initiative",
  "phase": "p1-foundations",
  "proposals": [],
  "review": {"verdict": "revise", "findings": [{"detail": "no column is read"}]},
  "run_id": "demo-initiative-2:p1-foundations",
  "ticket": "t1-probe"
}""")


def _write(runs: Path, run: str, record: dict) -> None:
    path = runs / run / "tasks" / PHASE / "t1-probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _drive_records(repo, cart, tmp_path, *, files=(), stored=()):  # noqa: F811
    """A run whose flagged t1-probe carries no attempt in its work item; `files` and `stored` are (run, record) pairs."""
    kinds = {"ticket_amend": {"risk": "low", "ramp": "eligible"}, "item_create": {"risk": "medium", "ramp": "gated"}}
    cart = {**cart, "write_kinds": {**cart["write_kinds"], **kinds}}
    work = initiative()
    assert not any(item.get("attempts") for item in work["items"])
    store = _store(work)
    for run, record in files:
        _write(tmp_path / "runs", run, record)
    for run, record in stored:
        store.record_task_record(run, PHASE, "t1-probe", record, "2026-08-30T10:00:00+00:00")
    runner = TriageClassRunner({t: new_file_patch(f"{t}.txt") for t in ("t1-probe", "t2-bench", "t3-cutover")}, _answer("hold"))
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=work, store=store, work_state="store", specs=TRIAGE_SPECS)
    return result, runner, store


def _answered(store, runner) -> str:
    """The triage prompt, once the answer was taken: the task is held with the mark, unbuilt, its flag a record."""
    row = _row(store, "t1-probe")
    assert TRIAGE_FLAG not in row["extra"]
    assert row["extra"][TRIAGE_RECORDS] == [{"class": "hold", "diagnosis": DIAGNOSIS, "run": "epic-1:p1-foundations:t1-probe"}]
    assert row["state"] == "blocked" and row["extra"][HOLD_REASON].startswith(HOLD_MARK)
    assert not _built(runner, "t1-probe")
    (call,) = _triage_calls(runner)
    return call["prompt"]


def test_a_flagged_task_with_no_file_attempt_is_triaged_from_its_run_record(repo, cart, tmp_path) -> None:  # noqa: F811
    _, runner, store = _drive_records(repo, cart, tmp_path, files=[("demo-initiative-2", RECORD)])
    prompt = _answered(store, runner)
    assert "'attempt-1|kind': None" in prompt  # a model made the cause, so no rule names the kind
    assert "'attempt-1|arbitration': 'revise'" in prompt and "'attempt-1|stopped': 'no change'" in prompt
    assert OBJECTION in prompt and "'attempt-1|evidence': [{'check': 'pytest', 'output': '1 passed'}]" in prompt


def test_a_record_another_lane_wrote_reaches_triage_through_the_store(repo, cart, tmp_path) -> None:  # noqa: F811
    older = {**RECORD, "date": "2026-08-29", "cause": "code", "cause_why": "rule: kind unverified"}
    _, runner, store = _drive_records(
        repo, cart, tmp_path, files=[("demo-initiative-2", RECORD)], stored=[("demo-initiative-1", older), ("demo-initiative-2", RECORD)],
    )
    prompt = _answered(store, runner)
    # Oldest first: the store's record of the other lane's run, then this machine's file; the run held in both is read once.
    assert "'attempt-1|kind': 'unverified'" in prompt and "'attempt-2|kind': None" in prompt and "attempt-3|" not in prompt


def test_only_the_records_of_this_initiative_that_name_a_cause_are_attempts(repo, cart, tmp_path) -> None:  # noqa: F811
    other = {**RECORD, "initiative": "other-initiative", "arbitration": {"verdict": "revise", "reasoning": "elsewhere"}}
    approved = {**RECORD, "cause": None, "cause_why": None}
    _, runner, store = _drive_records(
        repo, cart, tmp_path, files=[("other-initiative-4", other), ("demo-initiative-3", approved), ("demo-initiative-2", RECORD)],
    )
    prompt = _answered(store, runner)
    assert "attempt-1|" in prompt and "attempt-2|" not in prompt and "elsewhere" not in prompt


def test_a_flagged_task_with_no_record_still_fails_triage_and_stays_quarantined(repo, cart, tmp_path) -> None:  # noqa: F811
    result, runner, store = _drive_records(repo, cart, tmp_path)
    assert any(q["id"] == "t1-probe" for q in result["quarantined"])
    assert TRIAGE_FLAG in _row(store, "t1-probe")["extra"]
    assert not _built(runner, "t1-probe")
