import json
from pathlib import Path

import pytest

from agent_tools.chair_apply_fetch import apply_fetched_approvals, approved_task_ids

APPROVED = {"task": "t1", "review": {"verdict": "approve"}}
QUARANTINED = {**APPROVED, "status": "quarantined"}


def test_a_quarantined_record_with_approving_verdicts_is_not_approved():
    assert approved_task_ids([QUARANTINED]) == []


@pytest.mark.parametrize("cause", ["code", "checks", "merge", ""])
def test_a_quarantined_record_is_skipped_whatever_its_cause(cause: str):
    assert approved_task_ids([{**QUARANTINED, "cause": cause}]) == []


def test_the_same_record_without_a_quarantine_status_is_approved():
    assert approved_task_ids([APPROVED]) == ["t1"]


def test_a_fetched_quarantined_record_leaves_a_ready_ticket_ready(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    record = runs_dir / "run-1" / "tasks" / "p1" / "t1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(QUARANTINED), encoding="utf-8")
    ticket = tmp_path / "work" / "init" / "p1" / "t1.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("---\nstate: ready\n---\nbody\n", encoding="utf-8")
    ids, warnings = apply_fetched_approvals(runs_dir, tmp_path, "run-1", "init")
    assert ids == []
    assert warnings == []
    assert "state: ready" in ticket.read_text(encoding="utf-8")
