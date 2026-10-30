import argparse
import importlib
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from agent_tools import cli, land, route, route_sync, route_sync_gh, tracker

THRESHOLD = {"phases_min": 2, "tickets_min": 3, "multi_repo": True}


def _item(id, *, initiative=False, parent=None, issue=None, state="ready", phase="p1", parent_issue=None):
    return route_sync.Item(id=id, title=id, body="", repo="acme/w", state=state, phase=phase, run="", cost_usd=0.0,
                           gate="", issue=issue, parent=parent, parent_issue=parent_issue, initiative=initiative)


def _store(tasks=3, linked="401"):
    parent = "initiative:big"
    return [_item(parent, initiative=True, issue="900")] + [
        _item(f"t{n}", parent=parent, parent_issue="900", phase=f"p{n}", issue=linked if n == 0 else None)
        for n in range(tasks)]


def _ids(items):
    return [i.id for i in items]


def test_all_scope_keeps_every_item():
    assert _ids(route_sync.scoped(_store(), "all", THRESHOLD)) == ["initiative:big", "t0", "t1", "t2"]


def test_initiatives_scope_keeps_initiatives_and_linked_tasks_only():
    assert _ids(route_sync.scoped(_store(), "initiatives", THRESHOLD)) == ["initiative:big", "t0"]


def test_epics_scope_drops_an_initiative_under_the_threshold_but_keeps_a_linked_task():
    small = _store(tasks=1)
    assert _ids(route_sync.scoped(small, "epics", THRESHOLD)) == ["t0"]
    assert _ids(route_sync.scoped(_store(), "epics", THRESHOLD)) == ["initiative:big", "t0"]


def test_plan_under_epics_makes_no_task_issue_and_closes_a_landed_linked_task():
    items = route_sync.scoped([_item("initiative:big", initiative=True)] + [
        _item("t0", parent="initiative:big", issue="401", state="done"),
        _item("t1", parent="initiative:big"), _item("t2", parent="initiative:big", phase="p2")], "epics", THRESHOLD)
    steps = route_sync.plan(items, {"401": {"state": "OPEN", "title": "t0", "body": ""}}, {}, "github-projects", "epics")
    kinds = [s["kind"] for s in steps]
    assert [s["item_id"] for s in steps if s["kind"] in ("issue_create", "writeback")] == ["initiative:big"] * 2
    assert "issue_edit" not in kinds and "sub_issue_link" not in kinds
    assert steps[-1] == {"kind": "issue_close", "issue": "401"}


def test_plan_under_all_still_creates_a_task_issue():
    steps = route_sync.plan([_item("t1")], {}, {}, "github-projects")
    assert steps[0]["kind"] == "issue_create"


@pytest.mark.parametrize("task_issue, parent_issue, last, expected", [
    ("401", "900", False, "Part of #900\nCloses #401"),
    ("401", "900", True, "Closes #900\nCloses #401"),
    ("401", None, True, "Closes #401"),
    (None, "900", False, "Part of #900"),
    (None, "900", True, "Closes #900"),
    (None, None, True, None),
])
def test_pr_reference(task_issue, parent_issue, last, expected):
    assert land.pr_reference(task_issue, parent_issue, last) == expected


def test_initiative_facts_names_the_issue_and_the_last_task():
    items = _store()
    assert route_sync.initiative_facts(items, "epics", THRESHOLD, "initiative:big", "t1") == ("900", False)
    done = [i if i.id == "t1" else route_sync.Item(**{**i.__dict__, "state": "done"}) for i in items]
    assert route_sync.initiative_facts(done, "epics", THRESHOLD, "initiative:big", "t1") == ("900", True)
    assert route_sync.initiative_facts(_store(tasks=1), "epics", THRESHOLD, "initiative:big", "t0") == (None, False)


_RECORD = {"run": "r1", "task": "t1", "phase": "p1", "initiative": "big",
           "proposals": {"draft_pr_create": {"title": "x"}}}


def _body(steps):
    return next(s for s in steps if s["kind"] == "pr_create")["body"]


def test_land_plan_under_epics_has_no_presync_and_carries_the_reference(monkeypatch):
    monkeypatch.setattr(land, "_proposal", lambda record, kind: {"title": "x"})
    monkeypatch.setattr(land, "_approved", lambda record: None)
    branches = {"agents/r1/t1": ["subject"]}
    plain = land.land_plan(_RECORD, branches, "main", tracker="github-projects", scope="epics", reference="Part of #900")
    assert [s for s in plain if s.get("before")] == []
    assert "Part of #900" in _body(plain)
    assert plain[-1]["kind"] == "note"
    linked = land.land_plan(_RECORD, branches, "main", tracker="github-projects", issue="401", scope="epics",
                            reference="Closes #401")
    assert "Closes #401" in _body(linked)
    assert linked[-1] == {"kind": "route_sync", "item": "t1"}
    bare = land.land_plan(_RECORD, branches, "main", tracker="github-projects", scope="epics", reference=None)
    assert "Part of" not in _body(bare) and "Closes" not in _body(bare)


@pytest.mark.parametrize("value", ["/nonexistent/x", "justaname"])
def test_unresolvable_repo_is_refused_by_name(value):
    with pytest.raises(route_sync_gh.RepoError) as err:
        route_sync_gh._repo_of(value)
    assert str(err.value) == f"route sync: {value} is not a checkout with an origin remote nor an owner/name slug"
    assert route_sync_gh._repo_of("acme/widgets") == "acme/widgets"
    assert route_sync_gh._repo_of("") == ""


def test_the_land_watch_repo_still_passes_an_unresolvable_value_through():
    assert cli._gh_repo("/nonexistent/x") == "/nonexistent/x"


def _bad_initiative(tmp_path):
    _write(tmp_path / "work" / "big" / "initiative.md", "---\nid: big\ntitle: Big\nrepo: /nonexistent/x\n---\nb\n")
    _write(tmp_path / "work" / "big" / "p1" / "t1.md", "---\nid: t1\ntitle: T1\nissue: 401\n---\nb\n")


def test_an_unresolvable_initiative_repo_is_refused_by_name_with_no_gh_call(tmp_path, monkeypatch, capsys, gh_calls):
    _bad_initiative(tmp_path)
    monkeypatch.setattr(tracker, "tracker_for", lambda name: importlib.import_module("agent_tools.route_sync_gh"))
    assert cli._route_sync(_args(tmp_path, "tracker: github-projects\n")) == 2
    assert capsys.readouterr().out.splitlines() == [REFUSAL, "route sync: skipped 2 item(s): /nonexistent/x (2)"]
    assert gh_calls == []


def test_profile_tracker_scope_parses_and_rejects():
    assert route.parse_profile("tracker_scope: epics\n")["tracker_scope"] == "epics"
    assert "tracker_scope" not in route.parse_profile("tracker: none\n")
    with pytest.raises(route.ProfileError):
        route.parse_profile("tracker_scope: bogus\n")


class _Plugin:
    def sync(self, workspace, items, *, dry_run):
        raise AssertionError("sync must not run")


REFUSAL = "route sync: /nonexistent/x is not a checkout with an origin remote nor an owner/name slug"
SKIPPED = "route sync: skipped 1 item(s): /nonexistent/x (1)"


@pytest.fixture
def gh_calls(monkeypatch):
    """Every subprocess call a test makes; each one fails the test, since none of these paths may reach gh."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        raise AssertionError(f"unexpected subprocess call: {argv}")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(route_sync_gh, "auth_ok", lambda run: calls.append("auth") or True)
    return calls


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _args(tmp_path, profile_text, item=None):
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\n{profile_text}")
    return argparse.Namespace(item=item, workspace=str(tmp_path), dry_run=True, project=None, profile=str(profile),
                              runs_dir=None)


def _bad_intake(tmp_path):
    _write(tmp_path / "intake" / "bad.md", "---\nid: bad\ntitle: Bad\nrepo: /nonexistent/x\n---\nb\n")


def test_registered_path_refuses_an_unresolvable_intake_repo_by_name_with_no_gh_call(tmp_path, monkeypatch, capsys, gh_calls):
    _bad_intake(tmp_path)
    monkeypatch.setattr(tracker, "tracker_for", lambda name: _Plugin())
    assert cli._route_sync(_args(tmp_path, "tracker: plug\n")) == 2
    assert capsys.readouterr().out.splitlines() == [REFUSAL, SKIPPED]
    assert gh_calls == []


def test_gh_path_refuses_an_unresolvable_intake_repo_by_name_before_any_gh_call(tmp_path, monkeypatch, capsys, gh_calls):
    _bad_intake(tmp_path)
    monkeypatch.setattr(tracker, "tracker_for", lambda name: importlib.import_module("agent_tools.route_sync_gh"))
    assert cli._route_sync(_args(tmp_path, "tracker: github-projects\n")) == 2
    assert capsys.readouterr().out.splitlines() == [REFUSAL, SKIPPED]
    assert gh_calls == []


def test_a_bad_intake_file_does_not_refuse_the_sync_of_another_item(tmp_path):
    _bad_intake(tmp_path)
    _write(tmp_path / "intake" / "good.md", "---\nid: good\ntitle: Good\nrepo: acme/w\n---\nb\n")
    args = _args(tmp_path, "tracker: github-projects\n", item="good")
    items, scope, rc, skipped = cli._sync_items(args, str(tmp_path))
    assert (rc, scope, [i.id for i in items], skipped) == (None, "all", ["good"], 0)


def test_resolve_scope_lets_the_cartridge_win_and_refuses_an_unknown_value():
    assert route_sync.resolve_scope(None, {}) == "all"
    assert route_sync.resolve_scope("epics", {}) == "epics"
    assert route_sync.resolve_scope("epics", {"policy": {"tracker_scope": "initiatives"}}) == "initiatives"
    with pytest.raises(ValueError):
        route_sync.resolve_scope("all", {"policy": {"tracker_scope": "bogus"}})
    assert route.TRACKER_SCOPES == route_sync.SCOPES


def test_tracker_scope_reads_the_cartridge_and_its_threshold(monkeypatch):
    cartridge = {"policy": {"tracker_scope": "epics"}, "epic_threshold": {"tickets_min": 9}}
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: (cartridge, None))
    assert cli._tracker_scope({"tracker_scope": "all"}) == ("epics", {"tickets_min": 9}, None)


@pytest.mark.parametrize("named, expected", [(None, "all"), ("all", "all"), ("initiatives", "initiatives")])
def test_an_unresolvable_cartridge_leaves_the_scope_the_profile_names(tmp_path, named, expected):
    profile = {"team": "acme", "cartridges_dir": str(tmp_path), **({"tracker_scope": named} if named else {})}
    assert cli._tracker_scope(profile) == (expected, {}, None)


def test_an_unresolvable_cartridge_refuses_only_the_epics_scope_that_needs_its_threshold(tmp_path):
    scope, threshold, error = cli._tracker_scope({"team": "acme", "cartridges_dir": str(tmp_path), "tracker_scope": "epics"})
    assert (scope, threshold) == ("epics", {})
    assert error.startswith("cartridge acme cannot be resolved")


def test_tracker_scope_fails_closed_on_an_unknown_cartridge_scope(monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({"policy": {"tracker_scope": "bogus"}}, None))
    scope, _, error = cli._tracker_scope({})
    assert scope == "epics" and "'bogus' is not one of all, epics, initiatives" in error


def test_sync_under_epics_refuses_with_the_resolver_error_and_makes_no_gh_call(tmp_path, capsys, gh_calls):
    args = _args(tmp_path, f"tracker: github-projects\ntracker_scope: epics\nteam: acme\ncartridges_dir: {tmp_path}\n")
    assert cli._route_sync(args) == 2
    assert "route sync: cartridge acme cannot be resolved" in capsys.readouterr().out
    assert gh_calls == []


def test_sync_with_no_scope_set_and_an_unresolvable_cartridge_still_runs_under_all(tmp_path):
    args = _args(tmp_path, f"tracker: github-projects\nteam: acme\ncartridges_dir: {tmp_path}\n")
    assert cli._sync_items(args, str(tmp_path)) == ([], "all", None, 0)


_SRC = Path(route.__file__).parents[2]
_REAL_PROFILE = {"team": "local", "cartridges_dir": str(_SRC / "cartridges" / "cartridges"),
                 "skills_roots": [str(_SRC / "cartridges" / "skills-plugins")]}


def test_a_real_cartridge_gives_all_and_its_threshold_without_an_overlay():
    scope, threshold, error = cli._tracker_scope(_REAL_PROFILE)
    assert (scope, error) == ("all", None)
    assert threshold["tickets_min"] == 3


def test_a_real_team_cartridge_narrows_the_scope_over_the_profile_and_sets_the_threshold(tmp_path):
    _write(tmp_path / "acme" / "cartridge.yaml",
           "team: acme\nextends: local\nversion: 1\npolicy:\n  tracker_scope: epics\nepic_threshold:\n  tickets_min: 9\n")
    profile = {**_REAL_PROFILE, "team": "acme", "tracker_scope": "all", "cartridges_dir": str(tmp_path)}
    assert cli._team_cartridge(profile)[1] is None
    scope, threshold, error = cli._tracker_scope(profile)
    assert (scope, error) == ("epics", None)
    assert threshold["tickets_min"] == 9


def test_a_repo_overlay_the_cartridge_loader_refuses_is_an_unresolved_cartridge(tmp_path):
    _write(tmp_path / ".agent" / "cartridge.yaml", "policy:\n  tracker_scope: epics\n")
    assert cli._team_cartridge(_REAL_PROFILE, tmp_path)[1].startswith("cartridge local cannot be resolved")


def test_sync_of_an_out_of_scope_item_exits_zero_with_no_gh_call(tmp_path, monkeypatch, capsys, gh_calls):
    monkeypatch.setattr(route_sync_gh, "items_from_store", lambda workspace, *_, **__: _store(tasks=1, linked=None))
    assert cli._route_sync(_args(tmp_path, "tracker: github-projects\ntracker_scope: epics\n", item="t0")) == 0
    assert "t0 is out of scope under tracker_scope epics; nothing to mirror" in capsys.readouterr().out
    assert gh_calls == []


def test_the_land_sync_step_reports_an_out_of_scope_task_as_synced_not_failed(tmp_path, monkeypatch, gh_calls):
    monkeypatch.setattr(route_sync_gh, "items_from_store", lambda workspace, *_, **__: _store(tasks=1, linked=None))
    _args(tmp_path, "tracker: github-projects\ntracker_scope: epics\n")
    step = {"kind": "route_sync", "item": "t0", "workspace": str(tmp_path), "profile": str(tmp_path / "profile.yaml"),
            "runs_dir": None}
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert (ok, detail) == (True, "synced t0")
    assert gh_calls == []


def test_issues_written_back_by_an_earlier_sync_count_as_linked():
    written_back = [_item("initiative:old", initiative=True, issue="1")] + [
        _item(f"t{n}", parent="initiative:old", issue=str(100 + n)) for n in range(5)]
    assert len(route_sync.scoped(written_back, "epics", THRESHOLD)) == 6


def _land_facts(monkeypatch, items, cartridge=None):
    monkeypatch.setattr(cli, "_team_cartridge",
                        lambda profile, repo=None: (cartridge or {"policy": {"tracker_scope": "epics"}}, None))
    monkeypatch.setattr(route_sync_gh, "items_from_store", lambda workspace, *_: items)


_LAND_RECORD = {"initiative": "big", "task": "t1"}


def test_land_reference_is_part_of_the_initiative_while_other_tasks_are_open(monkeypatch):
    _land_facts(monkeypatch, _store())
    assert cli._land_reference({}, "ws", _LAND_RECORD, "t1", None) == ("epics", "Part of #900")


def test_land_reference_closes_the_initiative_on_its_last_task(monkeypatch):
    _land_facts(monkeypatch, [replace(i, state="done") if i.id != "t1" else i for i in _store()])
    assert cli._land_reference({}, "ws", _LAND_RECORD, "t1", None) == ("epics", "Closes #900")


def test_land_reference_closes_an_operator_linked_task_issue(monkeypatch):
    _land_facts(monkeypatch, _store())
    assert cli._land_reference({}, "ws", {**_LAND_RECORD, "task": "t0"}, "t0", "401") == ("epics", "Part of #900\nCloses #401")


def test_land_reference_closes_both_issues_for_a_linked_last_task(monkeypatch):
    _land_facts(monkeypatch, [replace(i, state="done") if i.id != "t0" else i for i in _store()])
    assert cli._land_reference({}, "ws", {**_LAND_RECORD, "task": "t0"}, "t0", "401") == ("epics", "Closes #900\nCloses #401")


def test_land_reference_is_none_for_a_task_in_an_unsynced_initiative(monkeypatch):
    _land_facts(monkeypatch, _store(tasks=1, linked=None))
    assert cli._land_reference({}, "ws", {**_LAND_RECORD, "task": "t0"}, "t0", None) == ("epics", None)


def test_land_reference_is_none_under_all(monkeypatch):
    _land_facts(monkeypatch, _store(), cartridge={"policy": {}})
    assert cli._land_reference({}, "ws", _LAND_RECORD, "t1", None) == ("all", None)


def test_land_reference_says_so_when_the_store_is_unreadable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({"policy": {"tracker_scope": "epics"}}, None))

    def unreadable(workspace, *_):
        raise OSError("disk gone")

    monkeypatch.setattr(route_sync_gh, "items_from_store", unreadable)
    assert cli._land_reference({}, "ws", _LAND_RECORD, "t1", None) == ("epics", None)
    assert "land: work store unreadable (OSError: disk gone); the PR carries no issue reference" in capsys.readouterr().err


def test_land_reference_fails_closed_to_epics_when_the_cartridge_is_unresolvable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({}, "cartridge acme cannot be resolved: boom"))
    assert cli._land_reference({"tracker_scope": "epics"}, "ws", _LAND_RECORD, "t1", "401") == ("epics", "Closes #401")
    assert "landing under tracker_scope epics" in capsys.readouterr().err


def _retired_store(tmp_path, old_state="done", old_tasks=3):
    _write(tmp_path / "work" / "old" / "initiative.md", "---\nid: old\ntitle: Old\nrepo: ~/repos/agent-tools\n---\nb\n")
    _write(tmp_path / "work" / "big" / "initiative.md", "---\nid: big\ntitle: Big\nrepo: acme/w\n---\nb\n")
    for n in range(1, old_tasks + 1):
        _write(tmp_path / "work" / "old" / f"p{n}" / f"o{n}.md", f"---\nid: o{n}\ntitle: O{n}\nstate: {old_state}\n---\nb\n")
    for n in (1, 2, 3):
        _write(tmp_path / "work" / "big" / f"p{n}" / f"t{n}.md", f"---\nid: t{n}\ntitle: T{n}\n---\nb\n")


class _Recorder:
    seen: ClassVar[list] = []

    def sync(self, workspace, items, *, dry_run):
        type(self).seen = [i.id for i in items]
        return True, []


@pytest.mark.parametrize("old_state", ["done", "dropped"])
def test_a_settled_initiative_with_a_retired_repo_is_skipped_and_the_open_epic_syncs(tmp_path, capsys, old_state):
    _retired_store(tmp_path, old_state)
    args = _args(tmp_path, "tracker: github-projects\ntracker_scope: epics\n")
    items, _, rc, skipped = cli._sync_items(args, str(tmp_path))
    assert (rc, skipped) == (None, 0)
    assert "initiative:big" in _ids(items) and "initiative:old" not in _ids(items)
    assert capsys.readouterr().out.splitlines() == ["route sync: skipped 1 item(s): ~/repos/agent-tools (1)"]
    steps = route_sync.plan(items, {}, {}, "github-projects", "epics")
    assert all("old" not in str(s.get("item_id", "")) for s in steps)


def test_an_open_item_with_a_retired_repo_is_skipped_by_name_and_the_others_sync(tmp_path, monkeypatch, capsys):
    _retired_store(tmp_path, old_state="todo")
    monkeypatch.setattr(tracker, "tracker_for", lambda name: _Recorder())
    assert cli._route_sync(_args(tmp_path, "tracker: plug\ntracker_scope: epics\n")) == 2
    out = capsys.readouterr().out.splitlines()
    assert out == ["route sync: ~/repos/agent-tools is not a checkout with an origin remote nor an owner/name slug",
                   "route sync: skipped 1 item(s): ~/repos/agent-tools (1)"]
    assert "initiative:big" in _Recorder.seen and "initiative:old" not in _Recorder.seen


def test_a_repo_alias_resolves_a_retired_path(tmp_path):
    profile = route.parse_profile("repo_aliases:\n  agent-tools: ppfenning/coxswain\ntracker: none\n")
    assert profile["repo_aliases"] == {"agent-tools": "ppfenning/coxswain"}
    aliases = profile["repo_aliases"]
    assert route_sync_gh._repo_of("~/repos/agent-tools", aliases) == "ppfenning/coxswain"
    assert route_sync_gh._repo_of("~/.local/share/coxswain/installs/agent-tools/", aliases) == "ppfenning/coxswain"
    assert route_sync_gh._repo_of("acme/w", aliases) == "acme/w"
    _retired_store(tmp_path, old_state="todo")
    repos = {i.id: i.repo for i in route_sync_gh.items_from_store(tmp_path, aliases)}
    assert repos["initiative:old"] == "ppfenning/coxswain"


def test_a_retired_repo_outside_the_scope_is_never_resolved(tmp_path, monkeypatch, capsys):
    _retired_store(tmp_path, old_tasks=1)
    seen = []
    monkeypatch.setattr(route_sync_gh, "_origin_slug", lambda value: seen.append(value) or "")
    args = _args(tmp_path, "tracker: github-projects\ntracker_scope: epics\n")
    _, _, rc, skipped = cli._sync_items(args, str(tmp_path))
    assert (rc, skipped, capsys.readouterr().out) == (None, 0, "")
    assert "~/repos/agent-tools" not in seen and "acme/w" in seen


def test_split_unresolved_settles_an_initiative_from_all_its_tasks_not_only_the_scoped_ones():
    bad = replace(_item("initiative:old", initiative=True), repo_error="e", repo_named="r")
    done_and_dropped = [bad, _item("o1", parent="initiative:old", state="done"),
                        _item("o2", parent="initiative:old", state="dropped")]
    assert route_sync.split_unresolved([bad], done_and_dropped) == ([], [bad], [])
    assert route_sync.split_unresolved([bad], [*done_and_dropped, _item("o3", parent="initiative:old")]) == ([], [], [bad])
    assert route_sync.split_unresolved([bad], [bad]) == ([], [], [bad])
