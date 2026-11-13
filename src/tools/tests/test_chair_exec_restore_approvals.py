import pytest

from agent_tools import chair_exec
from agent_tools.chair_exec import Deps, argv_for, perform

CAUSE = "hold released a ticket that was never rebuilt"
RESTORE = {"kind": "restore_approvals", "initiative": "i", "task_ids": ["t1", "t2"], "reason": CAUSE, "epoch": 1}


def _deps(tmp_path, calls: list) -> Deps:
    return Deps(
        run=lambda argv: calls.append(("run", argv)) or (0, ""),
        delete_branches=lambda repo, pattern: calls.append(("delete", repo, pattern)) or ([], ""),
        acquire_lease=lambda holder, host: calls.append(("lease", holder)) or "",
        record=lambda action: calls.append(("record", action["kind"])),
        run_id=lambda action: "run-1",
        repo_for=lambda action: action.get("repo", "r"),
        rev_parse=lambda repo, ref: None,
        work_dir=tmp_path,
        runs_dir=tmp_path / "runs",
        now=lambda: "2026-11-10T00:00:00+00:00",
    )


def _ticket(tmp_path, task_id: str, state: str = "ready"):
    path = tmp_path / "work" / "i" / "p1" / f"{task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: {task_id}\nstate: {state}\nsurfaces: [a.py]\n---\nBody.\n", encoding="utf-8")
    return path


def _mirror(monkeypatch, mirrored: list, result=None) -> None:
    monkeypatch.setattr(chair_exec.store_cli, "mirror_state", lambda *a: mirrored.append(a) or result)


def test_a_restore_rewrites_each_ticket_to_approved_mirrors_it_and_records_the_cause(tmp_path, monkeypatch) -> None:
    tickets = [_ticket(tmp_path, "t1"), _ticket(tmp_path, "t2")]
    mirrored: list = []
    _mirror(monkeypatch, mirrored)
    calls: list = []
    results = perform([RESTORE], _deps(tmp_path, calls), lambda: 1, False)
    assert [c for c in calls if c[0] != "record"] == []
    assert mirrored == [(tmp_path / "runs", "i", t, "approved", "chair") for t in ("t1", "t2")]
    assert (results[0]["status"], results[0]["reason"]) == ("done", f"restored t1, t2: {CAUSE}")
    assert all("state: approved" in t.read_text(encoding="utf-8") for t in tickets)


def test_a_mirror_warning_is_appended_to_the_reason(tmp_path, monkeypatch) -> None:
    _ticket(tmp_path, "t1")
    _mirror(monkeypatch, [], "warning: store did not record i/t1 as approved")
    results = perform([{**RESTORE, "task_ids": ["t1"]}], _deps(tmp_path, []), lambda: 1, False)
    assert (results[0]["status"], results[0]["reason"]) == (
        "done",
        f"restored t1: {CAUSE}; warning: store did not record i/t1 as approved",
    )


@pytest.mark.parametrize("fields", [{"task_ids": []}, {"initiative": ""}], ids=["no_task_ids", "no_initiative"])
def test_a_restore_without_an_initiative_or_task_ids_is_refused(tmp_path, monkeypatch, fields) -> None:
    mirrored: list = []
    _mirror(monkeypatch, mirrored)
    results = perform([{**RESTORE, **fields}], _deps(tmp_path, []), lambda: 1, False)
    assert results[0]["status"] == "refused"
    assert mirrored == []


def test_a_restore_fails_with_the_edits_reason_when_the_ticket_is_missing(tmp_path, monkeypatch) -> None:
    mirrored: list = []
    _mirror(monkeypatch, mirrored)
    results = perform([RESTORE], _deps(tmp_path, []), lambda: 1, False)
    assert (results[0]["status"], results[0]["reason"]) == ("failed", "ticket not found")
    assert mirrored == []


def test_a_done_ticket_fails_the_restore_and_nothing_is_written_or_mirrored(tmp_path, monkeypatch) -> None:
    first, second = _ticket(tmp_path, "t1"), _ticket(tmp_path, "t2", "done")
    before = (first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8"))
    mirrored: list = []
    _mirror(monkeypatch, mirrored)
    results = perform([RESTORE], _deps(tmp_path, []), lambda: 1, False)
    assert results[0]["status"] == "failed"
    assert mirrored == []
    assert (first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8")) == before


def test_a_restore_starts_no_process() -> None:
    assert argv_for(RESTORE) is None
