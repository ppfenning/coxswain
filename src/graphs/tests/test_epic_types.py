"""H2: the epic's record constructors read real shapes as they are and refuse malformed ones. No fakes: plain inputs."""

import re
from typing import get_type_hints

import pytest

from harness import epic_types
from harness.epic_types import MalformedRecord, attempt, attempt_row, phase_record, task_record

# A task record as `lifecycle` saves it, trimmed: the keys the driver reads, and one it does not.
RECORD = {
    "run_id": "i-3",
    "initiative": "i",
    "date": "2026-11-18",
    "cause": "ticket",
    "cause_why": "rule: kind refused",
    "build": {"patch": "diff --git a/x b/x\n", "evidence": [{"check": "pytest", "output": "ok"}]},
    "review": {"verdict": "reject", "rationale": "no test"},
    "arbitration": "reject",
    "proposals": [{"kind": "draft_pr_create"}],
    "failed_node": None,
    "tokens": {"in": 10, "out": 2},
}


def test_a_task_record_reads_as_saved_and_keeps_the_keys_it_does_not_name():
    record = task_record(RECORD)
    assert record == RECORD
    assert record is not RECORD  # a copy: the caller's value is never shared


@pytest.mark.parametrize(
    ("raw", "says"),
    [
        (["not", "an", "object"], "list is not an object"),
        ({**RECORD, "build": "patch"}, "build (str)"),
        ({**RECORD, "cause": 3}, "cause (int)"),
        ({**RECORD, "review": ["reject"]}, "review (list)"),
        ({**RECORD, "when": object()}, "object is not JSON"),
        ({**RECORD, 1: "x"}, "a key is not a string"),
    ],
)
def test_a_malformed_task_record_is_refused_with_what_is_wrong(raw, says):
    with pytest.raises(MalformedRecord, match=re.escape(says)):
        task_record(raw)


def test_an_attempts_row_reads_from_the_columns_a_select_named():
    cols = ("run_id", "phase_id", "kind", "reason", "ts", "cause")
    row = attempt_row(dict(zip(cols, ("i-2", "p1", "rescue_failed", "checks failed", "2026-11-18T10:00:00", None), strict=True)))
    assert row == {"run_id": "i-2", "phase_id": "p1", "kind": "rescue_failed", "reason": "checks failed",
                   "ts": "2026-11-18T10:00:00", "cause": None}  # fmt: skip
    assert attempt_row({"run_id": "i-2", "phase_id": "p1", "seq": 4, "cause": "harness"})["seq"] == 4
    with pytest.raises(MalformedRecord, match="no cause"):
        attempt_row({"run_id": "i-2", "phase_id": "p1"})
    with pytest.raises(MalformedRecord, match="seq"):
        attempt_row({"run_id": "i-2", "phase_id": "p1", "seq": "4", "cause": None})


def test_an_attempt_reads_from_a_work_file_and_refuses_one_with_no_run():
    entry = {"run": "i-2", "phase": "p1", "reason": "checks failed", "ts": "2026-11-18", "kind": "check_failed"}
    assert attempt({**entry, "review": {"verdict": "reject"}}) == {**entry, "review": {"verdict": "reject"}}
    with pytest.raises(MalformedRecord, match="no run"):
        attempt({k: v for k, v in entry.items() if k != "run"})
    with pytest.raises(MalformedRecord, match="patch_kept"):
        attempt({**entry, "patch_kept": "yes"})


def test_a_phase_record_reads_as_a_phase_built_it():
    record = {"phase": "p1", "parent": None, "branch": "epic/i/p1", "status": "complete", "rebased": False,
              "task_records": [{"id": "t1", "merged": True}], "quarantined": []}  # fmt: skip
    assert phase_record(record) == record
    with pytest.raises(MalformedRecord, match="no status"):
        phase_record({"phase": "p1"})
    with pytest.raises(MalformedRecord, match="rebased"):
        phase_record({**record, "rebased": "no"})


@pytest.mark.parametrize(
    ("record", "kinds"),
    [
        (epic_types.TaskRecord, epic_types._TASK_RECORD),
        (epic_types.AttemptRow, epic_types._ATTEMPT_ROW),
        (epic_types.Attempt, epic_types._ATTEMPT),
        (epic_types.PhaseRecord, epic_types._PHASE_RECORD),
    ],
)
def test_each_constructor_checks_every_key_its_record_names(record, kinds):
    assert set(get_type_hints(record)) == set(kinds)
