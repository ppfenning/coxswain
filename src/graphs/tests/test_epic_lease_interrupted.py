"""A lost lease records the phase and its tasks as interrupted: not quarantined, and not an attempt."""

from __future__ import annotations

import harness.epic as epic

CONFLICT = "CONFLICT (content): Merge conflict in pyproject.toml"
STALE = "stale epoch: this driver holds epoch 1, lease 'epic' is at epoch 2 or has expired"


def _record() -> dict:
    return {"phase": "p1", "status": "partial", "task_records": [{"id": "t1"}, {"id": "t2"}], "quarantined": []}


def test_the_stale_epoch_reason_is_recognised_and_a_conflict_is_not() -> None:
    assert epic._is_stale_epoch(STALE) is True
    assert epic._is_stale_epoch(CONFLICT) is False


def test_a_stale_epoch_phase_is_interrupted_with_nothing_quarantined() -> None:
    record = epic._stale_phase(_record(), STALE)

    assert record["status"] == "interrupted"
    assert record["quarantined"] == []
    assert record["reason"] == STALE


def test_a_stale_epoch_phase_lists_its_tasks_as_interrupted() -> None:
    record = epic._stale_phase(_record(), STALE)

    assert [(t["id"], t["grain"], t["reason"]) for t in record["interrupted"]] == [
        ("t1", "task", STALE),
        ("t2", "task", STALE),
    ]


def test_a_phase_stopped_before_any_task_ran_has_an_empty_interrupted_list() -> None:
    record = epic._stale_phase({**_record(), "task_records": []}, STALE)

    assert (record["status"], record["interrupted"], record["quarantined"]) == ("interrupted", [], [])


def test_an_interrupted_attempt_does_not_grow_the_attempt_tally() -> None:
    build = {"run": "r1", "kind": "refused"}
    interrupted = {"run": "r2", "kind": epic.INTERRUPTED_KIND}

    assert epic._build_attempts([build, interrupted]) == epic._build_attempts([build]) == [build]


def test_a_merged_task_and_a_task_quarantined_for_another_reason_are_not_interrupted() -> None:
    task_records = [
        {"id": "merged", "merged": True, "status": "built"},
        {"id": "held", "merged": False, "status": "quarantined", "quarantine": "checks failed"},
        {"id": "running", "merged": False, "status": "built"},
    ]
    held = {"id": "held", "phase": "p1", "grain": "task", "reason": "checks failed", "kind": "unverified"}
    record = epic._stale_phase({**_record(), "task_records": task_records, "quarantined": [held]}, STALE)

    assert [t["id"] for t in record["interrupted"]] == ["running"]
    assert record["quarantined"] == [held]


def test_a_task_quarantined_once_fenced_moves_from_quarantined_to_interrupted(monkeypatch) -> None:
    monkeypatch.setattr(epic, "_fenced", lambda _ctx: STALE)
    entry = epic._quarantine_task(None, {}, phase="p1", task="t1", reason="checks failed", kind="unverified")
    task_records = [{"id": "t1", "merged": False, "status": "quarantined", "quarantine": entry["reason"]}]
    record = epic._stale_phase({**_record(), "task_records": task_records, "quarantined": [entry]}, STALE)

    assert record["quarantined"] == []
    assert [(t["id"], t["kind"]) for t in record["interrupted"]] == [("t1", epic.INTERRUPTED_KIND)]
    assert "not recorded: checks failed" in record["interrupted"][0]["reason"]


def test_an_interrupted_phase_reaches_the_blocked_line_and_the_outcome() -> None:
    record = epic._stale_phase(_record(), STALE)

    assert epic.blocked_line(record) == f"phase p1: interrupted: {STALE}"
    outcome = epic.build_outcome({"phases": [record], "quarantined": [], "totals": {}})
    assert outcome["interrupted"] == [{"phase": "p1", "reason": STALE}]
    assert outcome["blocked"] == []


def test_a_non_stale_failure_is_still_quarantined() -> None:
    record = epic._stale_phase(_record(), CONFLICT)

    assert record["status"] == "blocked"
    assert [q["kind"] for q in record["quarantined"]] == ["no_work"]
    assert "interrupted" not in record
