import json
from pathlib import Path

import pytest

from agent_tools import chair_apply_fetch
from agent_tools.chair_apply_fetch import apply_fetched_approvals, apply_ready_to_approved, approved_task_ids


def test_a_record_that_names_its_task_under_ticket_is_approved_by_that_id():
    assert approved_task_ids([{"ticket": "t1", "review": {"verdict": "approve"}}]) == ["t1"]


def test_approved_task_ids_keeps_only_the_approved_records():
    records = [
        {"task": "t1", "review": {"verdict": "approve"}},
        {"task": "t2", "review": {"verdict": "revise"}},
        {"task": "t3"},
    ]
    assert approved_task_ids(records) == ["t1"]


def test_apply_ready_to_approved_moves_ready_to_approved():
    text = "---\nstate: ready\ntitle: x\n---\nbody\n"
    assert apply_ready_to_approved(text) == ("---\nstate: approved\ntitle: x\n---\nbody\n", None)


@pytest.mark.parametrize("state", ["approved", "done"])
def test_apply_ready_to_approved_is_a_no_op_once_already_moved(state: str):
    assert apply_ready_to_approved(f"---\nstate: {state}\n---\nbody\n") == (None, None)


def test_apply_ready_to_approved_refuses_any_other_state():
    new_text, note = apply_ready_to_approved("---\nstate: quarantined\n---\nbody\n")
    assert new_text is None
    assert note == "chair_apply_fetch: ticket state is 'quarantined', not moving to approved"


def _write_record(runs_dir: Path, run: str, phase: str, task: str, body) -> None:
    path = runs_dir / run / "tasks" / phase / f"{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def _write_ticket(work_dir: Path, initiative: str, phase: str, task: str, state: str) -> Path:
    path = work_dir / "work" / initiative / phase / f"{task}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstate: {state}\n---\nbody\n", encoding="utf-8")
    return path


def _fake_mirror(monkeypatch, warning=None) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(chair_apply_fetch.store_cli, "mirror_state", lambda *a: calls.append(a) or warning)
    return calls


def _approve(runs_dir: Path, task: str) -> None:
    _write_record(runs_dir, "run-1", "p1", task, {"task": task, "review": {"verdict": "approve"}})


def test_apply_fetched_approvals_moves_an_approved_ready_ticket_and_mirrors_it(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _approve(runs_dir, "t1")
    ticket = _write_ticket(work_dir, "init", "p1", "t1", "ready")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == (["t1"], [])
    assert "state: approved" in ticket.read_text(encoding="utf-8")
    assert calls == [(runs_dir, "init", "t1", "approved", "chair")]


def test_apply_fetched_approvals_leaves_a_revised_task_ready_unreturned_and_unmirrored(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _write_record(runs_dir, "run-1", "p1", "t1", {"task": "t1", "review": {"verdict": "revise"}})
    ticket = _write_ticket(work_dir, "init", "p1", "t1", "ready")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == ([], [])
    assert "state: ready" in ticket.read_text(encoding="utf-8")
    assert calls == []


@pytest.mark.parametrize("state", ["approved", "done"])
def test_apply_fetched_approvals_mirrors_a_ticket_already_past_ready_without_rewriting_it(tmp_path: Path, monkeypatch, state: str):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _approve(runs_dir, "t1")
    ticket = _write_ticket(work_dir, "init", "p1", "t1", state)
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == (["t1"], [])
    assert f"state: {state}" in ticket.read_text(encoding="utf-8")
    assert calls == [(runs_dir, "init", "t1", "approved", "chair")]


def test_apply_fetched_approvals_skips_a_ticket_in_another_state(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _approve(runs_dir, "t1")
    ticket = _write_ticket(work_dir, "init", "p1", "t1", "dropped")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == ([], [])
    assert "state: dropped" in ticket.read_text(encoding="utf-8")
    assert calls == []


def test_apply_fetched_approvals_mirrors_a_task_id_with_no_matching_ticket_file(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _approve(runs_dir, "t1")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == (["t1"], [])
    assert calls == [(runs_dir, "init", "t1", "approved", "chair")]


def test_apply_fetched_approvals_mirrors_an_unreadable_ticket_and_warns(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _approve(runs_dir, "t1")
    (work_dir / "work" / "init" / "p1" / "t1.md").mkdir(parents=True)
    ids, warnings = apply_fetched_approvals(runs_dir, work_dir, "run-1", "init")
    assert ids == ["t1"]
    assert len(warnings) == 1 and warnings[0].startswith("warning: t1: ticket file not updated: IsADirectoryError")
    assert calls == [(runs_dir, "init", "t1", "approved", "chair")]


def test_apply_fetched_approvals_mirrors_when_the_ticket_write_fails_and_does_not_raise(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _approve(runs_dir, "t1")
    _write_ticket(work_dir, "init", "p1", "t1", "ready")

    def refuse(self, *args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", refuse)
    ids, warnings = apply_fetched_approvals(runs_dir, work_dir, "run-1", "init")
    assert (ids, warnings) == (["t1"], ["warning: t1: ticket file not updated: PermissionError: read-only"])
    assert calls == [(runs_dir, "init", "t1", "approved", "chair")]


def test_apply_fetched_approvals_mirrors_a_task_named_by_two_records_once(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _approve(runs_dir, "t1")
    _write_record(runs_dir, "run-1", "p2", "t1", {"task": "t1", "review": {"verdict": "approve"}})
    _write_ticket(work_dir, "init", "p1", "t1", "ready")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == (["t1"], [])
    assert calls == [(runs_dir, "init", "t1", "approved", "chair")]


def test_apply_fetched_approvals_skips_a_malformed_task_record(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch)
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _write_record(runs_dir, "run-1", "p1", "t1", "{not json")
    _write_ticket(work_dir, "init", "p1", "t1", "ready")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == ([], [])
    assert calls == []


def test_apply_fetched_approvals_returns_a_mirror_warning_beside_the_id_and_does_not_raise(tmp_path: Path, monkeypatch):
    calls = _fake_mirror(monkeypatch, warning="warning: t1 -> approved: refused: no detail")
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _approve(runs_dir, "t1")
    _approve(runs_dir, "t2")
    ticket = _write_ticket(work_dir, "init", "p1", "t1", "ready")
    _write_ticket(work_dir, "init", "p1", "t2", "approved")
    ids, warnings = apply_fetched_approvals(runs_dir, work_dir, "run-1", "init")
    assert ids == ["t1", "t2"]
    assert warnings == ["warning: t1 -> approved: refused: no detail"] * 2
    assert "state: approved" in ticket.read_text(encoding="utf-8")
    assert [c[2] for c in calls] == ["t1", "t2"]
