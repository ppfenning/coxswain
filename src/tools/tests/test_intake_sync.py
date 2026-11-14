"""A source's tickets sync one way into the store's intake rows."""

from __future__ import annotations

from types import SimpleNamespace

from agent_tools import intake_sync

LINK = "https://forge.example/acme/app/issues/7"
AT = "2026-11-12T10:00:00+00:00"


def ticket(title: str = "Add a thing", body: str = "First text.") -> SimpleNamespace:
    return SimpleNamespace(title=title, body=body, link=LINK)


def intake(state: str = "queued", title: str = "Add a thing", body: str = "\nFirst text.\n", **extra) -> dict:
    return {"kind": "intake", "initiative": "intake", "task_id": "add-a-thing", "state": state, "title": title, "body": body,
            "extra": {"id": "add-a-thing", "link": LINK, **extra}}


def initiative() -> dict:
    return {"kind": "initiative", "initiative": "add-a-thing", "task_id": "add-a-thing", "state": "ready", "title": "Add a thing",
            "body": "plan", "extra": {"id": "add-a-thing", "repo": "app"}}


def test_a_queued_intake_takes_the_newer_text() -> None:
    (step,) = intake_sync.plan_sync([ticket(body="Second text.")], [intake()], AT)
    assert step.action == intake_sync.REFRESH
    (write,) = step.writes
    assert (write["initiative"], write["task_id"], write["title"], write["body"]) == ("intake", "add-a-thing", "Add a thing", "\nSecond text.\n")
    assert write["extra"]["upstream_digest"] == intake_sync.digest("Add a thing", "Second text.")
    assert write["extra"]["link"] == LINK, "the row's other fields are kept"
    assert "state" not in write, "a sync never moves a ticket's state"


def test_an_unchanged_ticket_is_remembered_once_and_then_costs_nothing() -> None:
    (step,) = intake_sync.plan_sync([ticket()], [intake()], AT)
    assert (step.action, step.line) == (intake_sync.BASELINE, "")
    seen = intake(upstream_digest=intake_sync.digest("Add a thing", "First text."))
    assert intake_sync.plan_sync([ticket()], [seen], AT) == []


def test_an_edit_after_decomposition_is_recorded_on_the_initiative_and_changes_no_text() -> None:
    seen = intake("decomposed", initiative="add-a-thing", upstream_digest=intake_sync.digest("Add a thing", "First text."))
    (step,) = intake_sync.plan_sync([ticket(title="Add two things", body="Second text.")], [seen, initiative()], AT)
    assert step.action == intake_sync.FLAG
    remembered, flagged = step.writes
    assert "title" not in remembered and "body" not in remembered
    assert remembered["extra"]["upstream_digest"] == intake_sync.digest("Add two things", "Second text.")
    assert (flagged["initiative"], flagged["task_id"]) == ("add-a-thing", "add-a-thing")
    assert flagged["extra"]["changed_upstream"] == {"at": AT, "link": LINK, "title": "Add two things"}
    assert flagged["extra"]["repo"] == "app" and "title" not in flagged and "body" not in flagged
    again = {**seen, "extra": remembered["extra"]}
    assert intake_sync.plan_sync([ticket(title="Add two things", body="Second text.")], [again, initiative()], AT) == [], "one edit, one record"


def test_the_first_sight_of_a_decomposed_ticket_claims_no_change() -> None:
    (step,) = intake_sync.plan_sync([ticket(body="Second text.")], [intake("decomposed", initiative="add-a-thing"), initiative()], AT)
    assert step.action == intake_sync.BASELINE and len(step.writes) == 1


def test_landed_and_done_intakes_and_tickets_nobody_filed_are_left_alone() -> None:
    assert intake_sync.plan_sync([ticket(body="Second text.")], [intake("landed"), initiative()], AT) == []
    assert intake_sync.plan_sync([ticket(body="Second text.")], [intake("done")], AT) == []
    assert intake_sync.plan_sync([ticket()], [], AT) == []


def test_a_ticket_with_no_body_is_filed_and_compared_by_its_title() -> None:
    row = intake(body="\nAdd a thing\n")
    (step,) = intake_sync.plan_sync([ticket(body="")], [row], AT)
    assert step.action == intake_sync.BASELINE


def _rig(monkeypatch, rows: list[dict], listing: list[dict], rc: int = 0):
    import json
    from pathlib import Path

    from agent_tools import cli, run_store, source_github

    writes: list[dict] = []
    monkeypatch.setattr(cli, "_run_argv", lambda argv: (rc, json.dumps(listing), "offline" if rc else ""))
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, *a, **k: rows)
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: writes.append(row) or "")
    config = SimpleNamespace(repos=("acme/app",), filter="", token_env="")
    return (lambda fresh=(), dry_run=False: cli._sync_source(source_github, config, Path("ws"), list(fresh), dry_run)), writes


def _raw(body: str) -> dict:
    return {"number": 7, "title": "Add a thing", "body": body, "labels": [{"name": "intake:taken"}], "milestone": None,
            "url": "https://github.com/acme/app/issues/7", "repository": {"nameWithOwner": "acme/app"}}


def test_the_taken_listing_asks_for_the_label_a_pull_leaves_behind() -> None:
    from agent_tools import source_github

    argv = source_github.taken_list_argv(SimpleNamespace(repos=("acme/app",), filter="", token_env=""), "acme/app")
    assert argv[:5] == ["gh", "issue", "list", "--repo", "acme/app"] and argv[argv.index("--label") + 1] == "intake:taken"


def test_a_pull_writes_the_newer_text_of_a_taken_ticket_to_the_store(monkeypatch, capsys) -> None:
    row = {**intake(), "extra": {"id": "add-a-thing", "link": "https://github.com/acme/app/issues/7"}}
    sync, writes = _rig(monkeypatch, [row], [_raw("Second text.")])
    assert sync() is False
    assert [w["body"] for w in writes] == ["\nSecond text.\n"]
    assert "took the newer text" in capsys.readouterr().out


def test_a_dry_run_says_what_it_would_do_and_writes_nothing(monkeypatch, capsys) -> None:
    row = {**intake(), "extra": {"id": "add-a-thing", "link": "https://github.com/acme/app/issues/7"}}
    sync, writes = _rig(monkeypatch, [row], [_raw("Second text.")])
    assert sync(dry_run=True) is False and writes == []
    assert capsys.readouterr().out.startswith("would sync: add-a-thing took the newer text")


def test_a_listing_that_fails_is_a_failed_sync_and_writes_nothing(monkeypatch, capsys) -> None:
    sync, writes = _rig(monkeypatch, [intake()], [], rc=1)
    assert sync() is True and writes == []
    assert "listing the taken tickets of acme/app failed" in capsys.readouterr().out
