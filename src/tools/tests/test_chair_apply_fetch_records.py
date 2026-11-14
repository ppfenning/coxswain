import json
from pathlib import Path

import pytest

from agent_tools import chair_apply_fetch, queue_export
from agent_tools.chair_apply_fetch import apply_fetched_approvals


def _setup(tmp_path: Path, monkeypatch, state: str) -> tuple[Path, Path]:
    monkeypatch.setattr(chair_apply_fetch.store_cli, "mirror_state", lambda *a: None)
    runs_dir = tmp_path / "runs"
    record = runs_dir / "run-1" / "tasks" / "p1" / "t1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"task": "t1", "review": {"verdict": "approve"}}), encoding="utf-8")
    ticket = tmp_path / "work" / "init" / "p1" / "t1.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(f"---\nstate: {state}\n---\nbody\n", encoding="utf-8")
    return runs_dir, ticket


def test_an_approved_ready_ticket_is_recorded_with_the_hash_of_its_new_text(tmp_path: Path, monkeypatch):
    runs_dir, ticket = _setup(tmp_path, monkeypatch, "ready")
    apply_fetched_approvals(runs_dir, tmp_path, "run-1", "init")
    new_text = ticket.read_text(encoding="utf-8")
    assert new_text == "---\nstate: approved\n---\nbody\n"
    assert queue_export.read_baseline_record(tmp_path) == {"work/init/p1/t1.md": queue_export.blob_hash(new_text)}


@pytest.mark.parametrize("state", ["approved", "quarantined"])
def test_a_ticket_left_unchanged_adds_no_baseline_entry(tmp_path: Path, monkeypatch, state: str):
    runs_dir, ticket = _setup(tmp_path, monkeypatch, state)
    before = ticket.read_text(encoding="utf-8")
    apply_fetched_approvals(runs_dir, tmp_path, "run-1", "init")
    assert ticket.read_text(encoding="utf-8") == before
    assert queue_export.read_baseline_record(tmp_path) == {}
