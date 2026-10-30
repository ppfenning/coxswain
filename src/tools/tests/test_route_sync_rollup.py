import argparse
import importlib

import pytest

from agent_tools import cli, queue_rows, route_sync, route_sync_gh, tracker

THRESHOLD = {"phases_min": 2, "tickets_min": 3, "multi_repo": True}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _task_file(title, state="ready", landed=None, release="2026.10.5"):
    """A task file as the store export writes it: yaml, so a `landed` dict is a block mapping."""
    row = {
        "kind": "task",
        "state": state,
        "title": title,
        "needs": [],
        "surfaces": [],
        "extra": {"landed": landed} if landed else {},
        "body": f"Target release: {release}.\n" if release else "b\n",
    }
    return queue_rows.render_item(row)


def _store(tmp_path, fixes):
    """`fixes` is `{name: (state, landed, release)}`; each is a one-task initiative."""
    for name, (state, landed, release) in fixes.items():
        _write(tmp_path / "work" / name / "initiative.md", "---\nrepo: acme/w\n---\n")
        _write(tmp_path / "work" / name / "p1" / f"{name}-1.md", _task_file(f"Fix {name}", state, landed, release))
    return route_sync_gh.items_from_store(tmp_path)


LANDED = {"at": "2026-10-30T01:00:00Z", "pr": 12}


def test_a_landed_pr_shows_from_a_store_shaped_record(tmp_path):
    items = _store(tmp_path, {"a": ("done", LANDED, "2026.10.5")})
    task = next(i for i in items if i.id == "a-1")
    assert task.landed_pr == "12"
    assert route_sync.checklist_line(task) == "- [x] Fix a: done, PR #12"


@pytest.mark.parametrize(
    "value, expected",
    [
        ({"at": "x", "pr": 7}, "7"),
        ({"pr": "https://github.com/acme/w/pull/10"}, "10"),
        ({"at": "x"}, ""),
        ({"at": "x", "pr": ""}, ""),
        (True, ""),
        (None, ""),
    ],
)
def test_landed_pr_of_reads_every_shape_the_store_hands_back(value, expected):
    assert route_sync.landed_pr_of(value) == expected


def test_the_block_is_rewritten_without_touching_the_surrounding_text():
    before, after = "Intro line.\n\n", "\n\nClosing line.\n"
    old = route_sync.replace_block("", ["- [ ] old: ready"])
    body = route_sync.replace_block(before + old + after, ["- [x] new: done, PR #3"])
    assert body.startswith(before) and body.endswith(after)
    assert "old: ready" not in body and "- [x] new: done, PR #3" in body
    assert route_sync.replace_block(body, ["- [x] new: done, PR #3"]) == body
    assert route_sync.replace_block("Intro.", ["- [ ] t: ready"]).startswith("Intro.\n\n" + route_sync.CHECKLIST_OPEN)


def test_a_synced_epic_body_carries_its_tasks_and_keeps_its_own_text(tmp_path):
    _write(tmp_path / "work" / "big" / "initiative.md", "---\nrepo: acme/w\n---\nWhy this matters.\n")
    for n in range(3):
        _write(
            tmp_path / "work" / "big" / "p1" / f"t{n}.md",
            _task_file(f"T{n}", "done" if n == 0 else "ready", LANDED if n == 0 else None, None),
        )
    items = route_sync_gh.items_from_store(tmp_path)
    epic = next(i for i in route_sync.with_checklists(items, items, "epics") if i.id == "initiative:big")
    assert epic.body.startswith("Why this matters.")
    assert "- [x] T0: done, PR #12" in epic.body and "- [ ] T1: ready" in epic.body
    assert route_sync.with_checklists(items, items, "all") == items


def _fixes(tmp_path):
    return _store(
        tmp_path, {"a": ("done", LANDED, "2026.10.5"), "b": ("ready", None, "2026.10.5"), "c": ("ready", None, None)}
    )


def test_one_task_fixes_for_a_release_share_one_issue_and_a_no_release_fix_stays_in_the_store(tmp_path):
    universe = _fixes(tmp_path)
    rollups = route_sync.rollup_items(universe, "epics", THRESHOLD)
    assert [r.title for r in rollups] == ["2026.10.5 fixes"]
    assert "- [x] Fix a: done, PR #12" in rollups[0].body and "- [ ] Fix b: ready" in rollups[0].body
    assert "Fix c" not in rollups[0].body
    assert route_sync.rollup_items(universe, "all", THRESHOLD) == []
    assert route_sync.rollup_for(universe, "epics", THRESHOLD, "a-1") == rollups[0]
    assert route_sync.rollup_for(universe, "epics", THRESHOLD, "initiative:c") is None


def test_a_second_sync_opens_no_new_issue(tmp_path):
    universe = _fixes(tmp_path)
    first = route_sync.plan(route_sync.rollup_items(universe, "epics", THRESHOLD), {}, {}, "github-projects", "epics")
    created = [s for s in first if s["kind"] == "issue_create"]
    assert [s["title"] for s in created] == ["2026.10.5 fixes"]
    on_file = {"77": {"title": created[0]["title"], "body": created[0]["body"], "state": "OPEN"}}
    items = route_sync.link_rollups(route_sync.rollup_items(universe, "epics", THRESHOLD), on_file)
    assert items[0].issue == "77"
    second = route_sync.plan(items, on_file, {}, "github-projects", "epics")
    assert not [s for s in second if s["kind"] in ("issue_create", "issue_edit")]


def test_a_settled_release_with_no_issue_is_not_opened(tmp_path):
    universe = _store(tmp_path, {"a": ("done", LANDED, "2026.10.5")})
    assert route_sync.link_rollups(route_sync.rollup_items(universe, "epics", THRESHOLD), {}) == []


def _epics(monkeypatch):
    monkeypatch.setattr(cli, "_tracker_scope", lambda profile, repo=None: ("epics", THRESHOLD, None))
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None: ({"epic_threshold": THRESHOLD}, None))


def _no_gh(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("unexpected gh lookup")

    monkeypatch.setattr(route_sync_gh, "rollup_issue_number", refuse)


def _record(initiative, task):
    return {"initiative": initiative, "task": task}


def test_a_roll_up_fix_land_says_part_of_the_release_issue(tmp_path, monkeypatch):
    _fixes(tmp_path)
    _epics(monkeypatch)
    monkeypatch.setattr(route_sync_gh, "rollup_issue_number", lambda run, rollup: "77")
    assert cli._land_reference({}, str(tmp_path), _record("a", "a-1"), "a-1", None) == ("epics", "Part of #77")
    assert cli._land_reference({}, str(tmp_path), _record("a", "a-1"), "a-1", "401") == (
        "epics",
        "Part of #77\nCloses #401",
    )


def test_a_roll_up_fix_land_opens_the_release_issue_when_it_is_not_on_file(tmp_path, monkeypatch):
    _fixes(tmp_path)
    _epics(monkeypatch)
    numbers, synced = iter([None, "78"]), []
    monkeypatch.setattr(route_sync_gh, "rollup_issue_number", lambda run, rollup: next(numbers))
    monkeypatch.setattr(cli, "_route_sync", lambda a: synced.append(a.item) or 0)
    sync = argparse.Namespace(item=None, dry_run=False)
    assert cli._land_reference({}, str(tmp_path), _record("a", "a-1"), "a-1", None, None, sync) == (
        "epics",
        "Part of #78",
    )
    assert synced == ["a-1"]


def test_a_dry_land_does_not_open_the_release_issue(tmp_path, monkeypatch):
    _fixes(tmp_path)
    _epics(monkeypatch)
    monkeypatch.setattr(route_sync_gh, "rollup_issue_number", lambda run, rollup: None)
    monkeypatch.setattr(cli, "_route_sync", lambda a: (_ for _ in ()).throw(AssertionError("synced")))
    assert cli._land_reference({}, str(tmp_path), _record("a", "a-1"), "a-1", None) == ("epics", None)


def test_an_epic_task_and_a_no_release_fix_keep_their_land_reference(tmp_path, monkeypatch):
    _epics(monkeypatch)
    _no_gh(monkeypatch)
    _write(tmp_path / "work" / "big" / "initiative.md", "---\nid: big\nrepo: acme/w\nissue: 900\n---\nb\n")
    for n in range(3):
        _write(tmp_path / "work" / "big" / "p1" / f"t{n}.md", _task_file(f"T{n}", "ready", None, None))
    assert cli._land_reference({}, str(tmp_path), _record("big", "t0"), "t0", None) == ("epics", "Part of #900")
    _store(tmp_path, {"c": ("ready", None, None)})
    assert cli._land_reference({}, str(tmp_path), _record("c", "c-1"), "c-1", None) == ("epics", None)


def _sync_args(tmp_path, monkeypatch, issues, item=None):
    """Arguments for a dry-run `route sync` under `epics` with `issues` on file; gh is never reached."""
    _epics(monkeypatch)
    monkeypatch.setattr(tracker, "tracker_for", lambda name: importlib.import_module("agent_tools.route_sync_gh"))
    monkeypatch.setattr(cli, "_resolve_sync_project", lambda *args: (None, None))
    monkeypatch.setattr(route_sync_gh, "auth_ok", lambda run: True)
    monkeypatch.setattr(route_sync_gh, "existing", lambda run, repos, project: (True, (issues, {}, {})))

    def viewed(*args):
        raise AssertionError("a roll-up has no issue to view")

    monkeypatch.setattr(route_sync_gh, "existing_item", viewed)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\ntracker: github-projects\ntracker_scope: epics\n")
    return argparse.Namespace(
        item=item, workspace=str(tmp_path), dry_run=True, project=None, profile=str(profile), runs_dir=None
    )


_ON_FILE = {"77": {"title": "2026.10.5 fixes", "body": "stale", "state": "OPEN"}}


def test_sync_opens_one_release_issue_and_a_second_sync_opens_none(tmp_path, monkeypatch, capsys):
    _fixes(tmp_path)
    assert cli._route_sync(_sync_args(tmp_path, monkeypatch, {})) == 0
    created = [line for line in capsys.readouterr().out.splitlines() if line.startswith("issue_create")]
    assert created == ["issue_create acme/w: '2026.10.5 fixes'"]
    assert cli._route_sync(_sync_args(tmp_path, monkeypatch, _ON_FILE)) == 0
    out = capsys.readouterr().out
    assert "issue_create" not in out and "issue_edit 77" in out


def test_sync_of_a_roll_up_task_finds_the_release_issue_by_title_not_by_view(tmp_path, monkeypatch, capsys):
    _fixes(tmp_path)
    assert cli._route_sync(_sync_args(tmp_path, monkeypatch, _ON_FILE, item="a-1")) == 0
    out = capsys.readouterr().out
    assert "issue_create" not in out and "issue_edit 77" in out


def test_sync_items_carries_the_epic_checklist_and_the_roll_up_but_not_a_no_release_fix(tmp_path, monkeypatch):
    _fixes(tmp_path)
    _write(tmp_path / "work" / "big" / "initiative.md", "---\nrepo: acme/w\n---\nWhy.\n")
    for n in range(3):
        _write(
            tmp_path / "work" / "big" / "p1" / f"t{n}.md",
            _task_file(f"T{n}", "done" if n == 0 else "ready", LANDED if n == 0 else None, None),
        )
    items, scope, rc, _ = cli._sync_items(_sync_args(tmp_path, monkeypatch, {}), str(tmp_path))
    by_id = {i.id: i for i in items}
    assert (scope, rc) == ("epics", None)
    assert sorted(by_id) == ["initiative:big", "release-fixes:2026.10.5"]
    assert by_id["initiative:big"].body.startswith("Why.") and "- [x] T0: done, PR #12" in by_id["initiative:big"].body
