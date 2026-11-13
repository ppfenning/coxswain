"""What one run task record says about an attempt at its task: the cause, the reason, the kind and the body it was built on.

Under a store the epic puts a quarantine's cause on the run's task record, `runs/<run>/tasks/<phase>/<task>.json`, and
nothing writes the attempt into the work file. The chair and the epic both read the attempt back from that record.
"""

from __future__ import annotations

import json

import pytest

from core import run_record, workstore

# The keys a live record carries, in the shapes `lifecycle-propose` and `harness.epic` write them.
CODE_RECORD = json.loads("""
{
  "adversary": {"objections": []},
  "arbitration": {"verdict": "approve", "reasoning": "the change does what the ticket asks"},
  "build": {"patch": "--- a/x.py\\n+++ b/x.py\\n", "files_touched": ["x.py"]},
  "cause": "code",
  "cause_why": "rule: kind unverified",
  "change_facts": {"checks": [
    {"name": "ruff", "cmd": "ruff check .", "passed": true, "exit_code": 0, "output_tail": ""},
    {"name": "pytest", "cmd": "pytest -q", "passed": false, "exit_code": 1,
     "output_tail": "collected 12 items\\nFAILED tests/test_x.py::test_short_row\\n1 failed, 11 passed"}
  ]},
  "date": "2026-11-12",
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
  "run_id": "demo-3:p1",
  "scope": {"state": "ready"},
  "ticket": "t1"
}
""")
REVIEW_RECORD = json.loads("""
{
  "adversary": {"objections": [{"claim": "the parser drops a short row"}]},
  "arbitration": {"verdict": "revise", "reasoning": "the parser drops a short row without a test. Add one."},
  "build": {"patch": ""},
  "cause": "review",
  "cause_why": "the reviewers held that the parser drops a short row",
  "date": "2026-11-12",
  "fix_loop": {"attempts": 3, "stopped": "no change"},
  "initiative": "demo",
  "phase": "p1",
  "proposals": [],
  "review": {"verdict": "revise"},
  "run_id": "demo-4:p1",
  "ticket": "t1"
}
""")


def test_a_code_quarantine_names_its_cause_its_kind_and_the_check_that_failed() -> None:
    attempt = run_record.record_attempt(CODE_RECORD)
    assert attempt is not None
    assert (attempt["cause"], attempt["cause_why"], attempt["kind"]) == ("code", "rule: kind unverified", "unverified")
    assert "pytest" in attempt["reason"] and "1 failed, 11 passed" in attempt["reason"]
    assert "ruff" not in attempt["reason"]


def test_a_review_quarantine_reads_the_model_why_and_the_fix_loop_in_the_record_own_words() -> None:
    attempt = run_record.record_attempt(REVIEW_RECORD)
    assert attempt is not None
    assert attempt["cause"] == "review"
    assert "kind" not in attempt  # a model made the cause, so no rule names the kind
    assert attempt["reason"].startswith("the reviewers held that the parser drops a short row")
    assert "the fix loop stopped: no change" in attempt["reason"]
    assert "the parser drops a short row without a test" in attempt["reason"]


def test_a_failed_rescue_is_named_by_its_rule() -> None:
    record = {"ticket": "t1", "phase": "p1", "cause": "code", "cause_why": "rule: rescue checks failed"}
    attempt = run_record.record_attempt(record)
    assert attempt is not None
    assert attempt["kind"] == run_record.RESCUE_KIND == "rescue_failed"
    assert attempt["reason"] == "rule: rescue checks failed"  # nothing else in the record says why


@pytest.mark.parametrize("cause", [None, "", "   "])
def test_a_record_with_no_cause_is_no_attempt(cause) -> None:
    # An approved build, or a run that never quarantined the task, leaves a record with no cause.
    assert run_record.record_attempt({**CODE_RECORD, "cause": cause}) is None


def test_a_record_that_is_not_an_object_is_no_attempt() -> None:
    assert run_record.record_attempt(["code"]) is None  # type: ignore[arg-type]


def test_the_body_a_record_was_built_on_is_named_only_when_the_record_carries_it() -> None:
    assert run_record.record_body_sha(CODE_RECORD) is None
    assert run_record.record_body_sha({"ticket_body": "\nBody.\n"}) == workstore.body_sha("Body.")
    assert run_record.record_body_sha({"body_sha": "abcdef012345"}) == "abcdef012345"
    assert run_record.record_body_sha({"body_sha": "", "ticket_body": None}) is None
