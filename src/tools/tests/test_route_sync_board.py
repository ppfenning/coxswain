import json
import re
import subprocess

from agent_tools import cli, route_sync, route_sync_gh


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _intake(root):
    path = root / "intake" / "t1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nid: t1\ntitle: T\n---\nbody\n")
    return path


def _board_run(calls, state_options=("Ready",), release_options=(), fail_set=False, milestones=""):
    """A `gh` fake for a board with a State field, a Release field unless `release_options` is None, and milestones."""
    release = list(release_options or ())

    def run(argv, **kw):
        calls.append(argv)
        # A created option or field shows up on the next `field-list`, as on the real board.
        release.extend(n for a in argv for n in re.findall(r'name:"([^"]+)"', a) if n not in release)
        if "--single-select-options" in argv:
            release.extend(argv[argv.index("--single-select-options") + 1].split(","))
        if argv[:3] == ["gh", "project", "view"]:
            return _Result(stdout=json.dumps({"id": "PVT_1"}))
        if argv[:3] == ["gh", "project", "field-list"]:
            fields = [{"name": "State", "id": "F_state", "options": [{"id": f"O_{n}", "name": n} for n in state_options]}]
            fields += [{"name": n, "id": f"F_{n}"} for n in ("Phase", "Run", "Cost", "Gate", "PR")]
            if release_options is not None:
                fields.append({"name": "Release", "id": "F_rel",
                               "options": [{"id": f"R_{n}", "name": n} for n in release]})
            return _Result(stdout=json.dumps({"fields": fields}))
        if argv[:3] == ["gh", "issue", "create"]:
            return _Result(stdout="https://github.com/acme/widgets/issues/9")
        if argv[:3] == ["gh", "project", "item-add"]:
            return _Result(stdout=json.dumps({"id": "PVTI_9"}))
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return _Result(stdout=milestones)
        if fail_set and any("updateProjectV2ItemFieldValue" in a for a in argv):
            return _Result(returncode=1, stderr="boom")
        return _Result(stdout="{}")
    return run


def _steps(*more):
    create = {"kind": "issue_create", "repo": "acme/widgets", "title": "T", "body": "b", "label": "coxswain",
              "item_id": "t1"}
    return [create, {"kind": "project_add", "issue": None}, *more]


def _set(field, value):
    return {"kind": "project_set", "issue": None, "field": field, "value": value}


def _milestone(title):
    return {"kind": "milestone_set", "repo": "acme/widgets", "issue": None, "title": title}


def _field_updates(calls):
    return [c for c in calls if any("updateProjectV2Field(" in a for a in c)]


def test_a_missing_state_option_refuses_before_any_gh_mutation(tmp_path):
    path, calls = _intake(tmp_path), []
    log = route_sync_gh.execute(_steps(_set("State", "Landed")), _board_run(calls), "acme/7", tmp_path)
    assert len(log) == 1 and log[0][:2] == ("state_option", False) and "'Landed'" in log[0][2]
    assert not [c for c in calls if c[:3] == ["gh", "issue", "create"]]
    assert "issue:" not in path.read_text()


def test_the_issue_number_survives_a_failed_state_set(tmp_path):
    path = _intake(tmp_path)
    steps = _steps(_set("State", "Ready"), {"kind": "writeback", "item_id": "t1", "issue": None})
    log = route_sync_gh.execute(steps, _board_run([], fail_set=True), "acme/7", tmp_path)
    assert [(kind, ok) for kind, ok, _ in log] == [("issue_create", True), ("project_add", True), ("project_set", False)]
    assert "issue: 9" in path.read_text()


def test_parse_release_reads_only_the_body_line():
    assert route_sync.parse_release("intro\nTarget release: 2026.10.5.\nmore") == "2026.10.5"
    assert route_sync.parse_release("Target release: 2026.10.5") == "2026.10.5"
    assert route_sync.parse_release("release: 2026.10.5\nsee Target release: 2026.10.5. here") == ""


def test_epic_release_compares_dotted_integers():
    assert route_sync.epic_release(["2026.10.5", "", "2026.10.10"]) == "2026.10.10"
    assert route_sync.epic_release([]) == ""


def test_an_epic_takes_the_latest_task_release_and_only_the_epic_gets_a_milestone(tmp_path):
    config = tmp_path / "checkout" / ".git" / "config"
    config.parent.mkdir(parents=True)
    config.write_text('[remote "origin"]\n\turl = git@github.com:acme/widgets.git\n')
    for path, text in (("initiative.md", f"---\nrepo: {tmp_path / 'checkout'}\n---\n"),
                       ("build/a.md", "---\ntitle: a\nstate: ready\n---\nTarget release: 2026.10.5.\n"),
                       ("build/b.md", "---\ntitle: b\nstate: ready\n---\nTarget release: 2026.10.10.\n")):
        (tmp_path / "work" / "init1" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "work" / "init1" / path).write_text(text)
    items = {i.id: i for i in route_sync_gh.items_from_store(tmp_path)}
    assert items["a"].release == "2026.10.5" and items["initiative:init1"].release == "2026.10.10"
    steps = route_sync.plan(list(items.values()), {}, {}, "github-projects")
    assert [s["title"] for s in steps if s["kind"] == "milestone_set"] == ["2026.10.10"]
    assert ("Release", "2026.10.5") in [(s["field"], s["value"]) for s in steps if s["kind"] == "project_set"]


def test_a_target_release_sets_the_field_and_the_milestone_and_creates_a_missing_option(tmp_path):
    _intake(tmp_path)
    calls = []
    steps = _steps(_set("Release", "2026.10.5"), _milestone("2026.10.5"))
    log = route_sync_gh.execute(steps, _board_run(calls, release_options=("2026.9.1",)), "acme/7", tmp_path)
    assert [(kind, ok) for kind, ok, _ in log] == [
        ("issue_create", True), ("project_add", True), ("project_set", True), ("milestone_set", True)]
    updates = _field_updates(calls)
    assert len(updates) == 1 and '"2026.9.1"' in updates[0][4] and '"2026.10.5"' in updates[0][4]
    assert calls.index(updates[0]) < calls.index(next(c for c in calls if c[:3] == ["gh", "issue", "create"]))
    assert ["gh", "api", "repos/acme/widgets/milestones", "-f", "title=2026.10.5"] in calls
    assert ["gh", "issue", "edit", "9", "--repo", "acme/widgets", "--milestone", "2026.10.5"] in calls


def test_an_existing_release_option_and_milestone_are_not_created_again(tmp_path):
    _intake(tmp_path)
    calls = []
    steps = _steps(_set("Release", "2026.10.5"), _milestone("2026.10.5"))
    run = _board_run(calls, release_options=("2026.10.5",), milestones="2026.10.5\n")
    log = route_sync_gh.execute(steps, run, "acme/7", tmp_path)
    assert all(ok for _, ok, _ in log)
    assert not _field_updates(calls)
    assert ["gh", "api", "repos/acme/widgets/milestones", "-f", "title=2026.10.5"] not in calls


def _cli_workspace(root, task_state, body=""):
    """A mirrored workspace with one initiative and one task, whose repo resolves to acme/widgets."""
    config = root / "checkout" / ".git" / "config"
    config.parent.mkdir(parents=True)
    config.write_text('[remote "origin"]\n\turl = git@github.com:acme/widgets.git\n')
    (root / "runs").mkdir()
    (root / "runs" / "policy.tracker.json").write_text('{"tracker": "github-projects"}')
    (root / "work" / "init1" / "build").mkdir(parents=True)
    (root / "work" / "init1" / "initiative.md").write_text(f"---\nrepo: {root / 'checkout'}\n---\n")
    (root / "work" / "init1" / "build" / "t.md").write_text(f"---\nid: t\ntitle: T\nstate: {task_state}\n---\n{body}\n")


def test_cli_route_sync_exits_1_naming_the_missing_state_option_and_creates_nothing(tmp_path, monkeypatch, capsys):
    _cli_workspace(tmp_path, "done")
    calls = []
    monkeypatch.setattr(subprocess, "run", _board_run(calls, state_options=("Ready",)))
    rc = cli.main(["route", "sync", "--project", "acme/7", "--workspace", str(tmp_path)])
    assert rc == 1 and "'Landed'" in capsys.readouterr().out
    assert not [c for c in calls if c[:3] == ["gh", "issue", "create"]]


def test_cli_route_sync_sets_the_release_field_and_the_epic_milestone(tmp_path, monkeypatch):
    _cli_workspace(tmp_path, "ready", "Target release: 2026.10.5.")
    calls = []
    monkeypatch.setattr(subprocess, "run", _board_run(calls, release_options=("2026.9.1",)))
    rc = cli.main(["route", "sync", "--project", "acme/7", "--workspace", str(tmp_path)])
    assert rc == 0
    assert len(_field_updates(calls)) == 1
    assert ["gh", "api", "repos/acme/widgets/milestones", "-f", "title=2026.10.5"] in calls
    editing = [c for c in calls if c[:3] == ["gh", "issue", "edit"] and "--milestone" in c]
    assert len(editing) == 1


def test_a_board_without_a_release_field_gets_one_made_with_the_option(tmp_path):
    _intake(tmp_path)
    calls = []
    route_sync_gh.execute(_steps(_set("Release", "2026.10.5")), _board_run(calls, release_options=None), "acme/7", tmp_path)
    assert ["gh", "project", "field-create", "7", "--owner", "acme", "--name", "Release", "--data-type",
            "SINGLE_SELECT", "--single-select-options", "2026.10.5"] in calls
