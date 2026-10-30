import argparse
import importlib
import json
import re
import subprocess

import pytest

from agent_tools import cli, route_sync, route_sync_gh, tracker

THRESHOLD = {"phases_min": 2, "tickets_min": 3, "multi_repo": True}
_WRITES = {("gh", "issue", "close"), ("gh", "project", "item-delete")}
_CAVEAT = "route sync: every open `coxswain`-labelled task issue counts as made by sync, one labelled by hand too"


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _store(tmp_path):
    """One epic with issue 900 and four tasks: 401 and 402 made by a sync, 403 linked by hand, 404 landed."""
    _write(tmp_path / "work" / "big" / "initiative.md", "---\nid: big\ntitle: Big\nrepo: acme/w\nissue: 900\n---\nb\n")
    for name, issue, state in (("t0", 401, "todo"), ("t1", 402, "todo"), ("t2", 403, "todo"), ("t3", 404, "done")):
        _write(tmp_path / "work" / "big" / "p1" / f"{name}.md",
               f"---\nid: {name}\ntitle: {name.upper()}\nissue: {issue}\nstate: {state}\n---\nb\n")


def _issue(number, state="OPEN"):
    return {"number": number, "title": str(number), "body": "", "state": state}


def _card(number, repo="acme/w"):
    return {"id": f"PVTI_{number}", "content": {"number": number, "repository": repo}, "state": "Ready"}


def _gh(monkeypatch, issues, cards):
    """A fake gh. The issue list holds only labelled issues, as `gh issue list --label coxswain` does."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["gh", "issue", "list"]:
            return _Result(stdout=json.dumps(issues))
        if argv[:3] == ["gh", "project", "item-list"]:
            return _Result(stdout=json.dumps({"items": cards}))
        return _Result()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(route_sync_gh, "auth_ok", lambda run: True)
    monkeypatch.setattr(tracker, "tracker_for", lambda name: importlib.import_module("agent_tools.route_sync_gh"))
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({"epic_threshold": THRESHOLD}, None))
    return calls


def _args(tmp_path, scope, dry_run, tracker_name="github-projects", item=None):
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\ntracker: {tracker_name}\ntracker_scope: {scope}\n")
    return argparse.Namespace(item=item, workspace=str(tmp_path), dry_run=dry_run, project="acme/7",
                              profile=str(profile), runs_dir=None, prune_out_of_scope=True)


def _writes(calls):
    return [c[:3] for c in calls if tuple(c[:3]) in _WRITES]


@pytest.fixture
def board(tmp_path, monkeypatch):
    _store(tmp_path)
    issues = [_issue(900), _issue(401), _issue(402), _issue(404)]
    cards = [_card(900), _card(401)]
    return tmp_path, _gh(monkeypatch, issues, cards)


def _listed(out):
    return re.findall(r"^issue_prune (\d+) ", out, re.MULTILINE)


def test_dry_run_under_epics_lists_task_issues_and_their_cards_and_writes_nothing(board, capsys):
    tmp_path, calls = board
    assert cli._route_sync(_args(tmp_path, "epics", True)) == 0
    assert capsys.readouterr().out.splitlines() == [
        _CAVEAT, "issue_prune 401 acme/w: 'T0' (close as not planned)", "card_remove 401 acme/w",
        "issue_prune 402 acme/w: 'T1' (close as not planned)"]
    assert _writes(calls) == []


def test_linked_landed_and_epic_issues_are_never_listed(board, capsys):
    tmp_path, _ = board
    cli._route_sync(_args(tmp_path, "epics", True))
    out = capsys.readouterr().out
    assert [n for n in ("403", "404", "900") if n in out] == []


def _item(id, **kw):
    return route_sync.Item(id=id, title=id, body="", state="todo", phase="p1", run="", cost_usd=0.0, gate="",
                           **{"repo": "acme/w", "issue": None, **kw})


def test_prune_steps_skips_a_task_sharing_its_epics_issue_and_lists_nothing_under_all():
    items = [_item("e", issue="9", initiative=True), _item("t", issue="9", parent="e"), _item("u", issue="8", parent="e")]
    issues = {("acme/w", "9"): {"state": "OPEN"}, ("acme/w", "8"): {"state": "OPEN"}}
    assert [s["issue"] for s in route_sync.prune_steps(items, issues, {}, "initiatives")] == ["8"]
    assert route_sync.prune_steps(items, issues, {}, "all") == []


def test_a_labelled_issue_in_another_repo_never_makes_a_hand_linked_issue_prunable():
    items = [_item("e", initiative=True, repo="acme/a"), _item("t", issue="5", parent="e", repo="acme/a")]
    labelled_elsewhere = {("acme/b", "5"): {"state": "OPEN"}}
    assert route_sync.prune_steps(items, labelled_elsewhere, {("acme/b", "5"): "PVTI_5"}, "epics") == []


def test_a_card_is_listed_for_a_closed_sync_made_issue_without_a_close():
    items = [_item("e", initiative=True), _item("t", issue="5", parent="e")]
    steps = route_sync.prune_steps(items, {("acme/w", "5"): {"state": "CLOSED"}}, {("acme/w", "5"): "PVTI_5"}, "epics")
    assert steps == [{"kind": "card_remove", "issue": "5", "repo": "acme/w", "card": "PVTI_5"}]


def test_a_real_run_closes_only_what_the_dry_run_listed(board, capsys):
    tmp_path, calls = board
    cli._route_sync(_args(tmp_path, "epics", True))
    listed = _listed(capsys.readouterr().out)
    calls.clear()
    assert cli._route_sync(_args(tmp_path, "epics", False)) == 0
    closes = [c for c in calls if c[:3] == ["gh", "issue", "close"]]
    assert [c[3] for c in closes] == listed == ["401", "402"]
    assert all(c[4:8] == ["--repo", "acme/w", "--reason", "not planned"] and "--comment" in c for c in closes)
    assert [c for c in calls if c[:3] == ["gh", "project", "item-delete"]] == [
        ["gh", "project", "item-delete", "7", "--owner", "acme", "--id", "PVTI_401"]]
    assert len(_writes(calls)) == 3


def test_a_second_real_run_closes_nothing_once_the_issues_are_closed(tmp_path, monkeypatch, capsys):
    _store(tmp_path)
    calls = _gh(monkeypatch, [_issue(900), _issue(401, "CLOSED"), _issue(402, "CLOSED")], [])
    assert cli._route_sync(_args(tmp_path, "epics", False)) == 0
    assert _writes(calls) == []
    assert "nothing is out of scope" in capsys.readouterr().out


def test_a_card_left_by_a_failed_delete_is_removed_on_the_next_run(tmp_path, monkeypatch):
    _store(tmp_path)
    calls = _gh(monkeypatch, [_issue(900), _issue(401, "CLOSED")], [_card(401)])
    assert cli._route_sync(_args(tmp_path, "epics", False)) == 0
    assert _writes(calls) == [["gh", "project", "item-delete"]]


def test_under_all_nothing_is_out_of_scope(board, capsys):
    tmp_path, calls = board
    assert cli._route_sync(_args(tmp_path, "all", False)) == 0
    assert "nothing is out of scope under tracker_scope all" in capsys.readouterr().out
    assert _writes(calls) == []


def test_item_is_refused_with_exit_2(board, capsys):
    tmp_path, calls = board
    assert cli._route_sync(_args(tmp_path, "epics", True, item="t0")) == 2
    assert "takes no --item" in capsys.readouterr().out
    assert _writes(calls) == []


def test_a_registered_tracker_is_refused_with_exit_2(board, monkeypatch, capsys):
    tmp_path, calls = board
    monkeypatch.setattr(tracker, "tracker_for", lambda name: object())
    assert cli._route_sync(_args(tmp_path, "epics", False, tracker_name="plug")) == 2
    assert "needs the github-projects tracker" in capsys.readouterr().out
    assert _writes(calls) == []
