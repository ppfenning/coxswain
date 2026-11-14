"""The land and launch commands cut from the initiative's target branch, and the chair reads the epic files the same way."""

import json
from datetime import UTC, datetime

import pytest
from test_land import _record
from test_route_cli import _init_repo, _unmeasured_window, _write_harness, _write_launch_profile

from agent_tools import base_branch, chair_facts, cli, usage_window

pytestmark = pytest.mark.usefixtures("board_store")  # the store is the board; these launches read its rows

_REAL_TEAM_CARTRIDGE = cli._team_cartridge


class _Stop(Exception):
    pass


@pytest.fixture(autouse=True)
def _no_usage_gather(monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)


@pytest.fixture(autouse=True)
def _no_cartridge(monkeypatch):
    """The profiles here name a team whose cartridge does not exist; a test that wants one sets `cartridge`."""
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None, overlay_keys=None: ({}, None))


def _store(tmp_path, initiative_text="---\nid: x\ntitle: X\nrepo: R\n---\n\nBody\n", epics=None):
    """A work store with `x` as the one initiative and an `epics/` file per `epics` entry."""
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "work" / "x").mkdir(parents=True)
    (ws / "work" / "x" / "initiative.md").write_text(initiative_text)
    for name, text in (epics or {}).items():
        (ws / "epics").mkdir(exist_ok=True)
        (ws / "epics" / f"{name}.md").write_text(text)
    return ws


def _epic(name, branch):
    return f"---\nid: {name}\ntitle: T\ntarget_branch: {branch}\n---\n"


def _initiative(**fields):
    return "---\nid: x\ntitle: X\nrepo: R\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n\nBody\n"


# --- the shared resolver -------------------------------------------------------------------------------------------


def test_an_initiative_value_beats_its_epic_and_the_epic_beats_nothing(tmp_path):
    ws = _store(tmp_path, _initiative(epic="e", target_branch="own"), {"e": _epic("e", "release")})
    assert cli._initiative_target_branch({}, None, ws, "x") == ("own", None)
    ws = _store(tmp_path / "b", _initiative(epic="e"), {"e": _epic("e", "release")})
    assert cli._initiative_target_branch({}, None, ws, "x") == ("release", None)


def test_the_cartridge_target_branch_is_the_third_level(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None, overlay_keys=None: ({"landing_areas": {"target_branch": "next"}}, None))
    assert cli._initiative_target_branch({}, None, _store(tmp_path), "x") == ("next", None)


@pytest.mark.parametrize(
    "initiative_text, epics, notes",
    [
        (_initiative(epic="ghost"), None, 1),
        (_initiative(epic="[a, b]"), None, 1),
        ("no front matter here\n", None, 1),
        (_initiative(epic="e"), {"e": "---\nid: e\ntitle: [unclosed\n---\n"}, 1),
        (_initiative(), None, 0),
    ],
)
def test_a_gap_with_no_target_branch_set_falls_back_with_one_note_and_no_refusal(tmp_path, capsys, initiative_text, epics, notes):
    ws = _store(tmp_path, initiative_text, epics)
    assert cli._initiative_target_branch({}, None, ws, "x") == (None, None)
    err = capsys.readouterr().err.splitlines()
    assert len(err) == notes and all(line.startswith("note:") for line in err)


def test_an_unreadable_epics_file_is_a_note_and_an_empty_result(tmp_path, capsys):
    ws = _store(tmp_path, epics={"e": "---\nid: other\n---\n"})
    assert cli._epic_definitions(ws) == {}
    assert capsys.readouterr().err.startswith("note: epic file ")


def test_a_missing_or_empty_epics_directory_means_no_epic_and_no_note(tmp_path, capsys):
    ws = _store(tmp_path)
    assert cli._epic_definitions(ws) == {}
    (ws / "epics").mkdir()
    assert cli._epic_definitions(ws) == {}
    assert capsys.readouterr().err == ""


def test_a_bad_target_branch_on_the_initiative_is_a_refusal(tmp_path):
    branch, refusal = cli._initiative_target_branch({}, None, _store(tmp_path, _initiative(target_branch="bad name")), "x")
    assert branch is None and "initiative target_branch must be a valid branch name" in refusal


def test_an_unresolvable_cartridge_is_the_resolvers_message(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None, overlay_keys=None: ({}, "cartridge acme cannot be resolved: boom"))
    assert cli._initiative_target_branch({}, None, _store(tmp_path), "x") == (None, "cartridge acme cannot be resolved: boom")


# --- the chair and the CLI agree -----------------------------------------------------------------------------------


def _gather_args(monkeypatch):
    seen = {}
    monkeypatch.setattr(chair_facts, "gather_facts", lambda deps, now, epic_definitions=None: seen.setdefault("defs", epic_definitions))
    return seen


def test_the_definitions_reach_the_gatherer(tmp_path, monkeypatch):
    seen = _gather_args(monkeypatch)
    ws = _store(tmp_path, epics={"e": _epic("e", "release")})
    cli._chair_gather(ws)(object(), datetime.now(UTC))
    assert seen["defs"] == {"e": {"id": "e", "title": "T", "target_branch": "release"}}


def test_the_chair_and_the_cli_agree_on_an_unreadable_epics_file(tmp_path, monkeypatch, capsys):
    seen = _gather_args(monkeypatch)
    ws = _store(tmp_path, _initiative(epic="e"), {"e": "---\nid: other\n---\n"})
    cli._chair_gather(ws)(object(), datetime.now(UTC))
    assert seen["defs"] == {}
    assert cli._initiative_target_branch({}, None, ws, "x") == (None, None)
    assert capsys.readouterr().err.count("note: epic file ") == 2


def test_the_chair_and_the_cli_agree_on_an_epic_file_with_a_bad_branch_name(tmp_path, monkeypatch):
    _gather_args(monkeypatch)
    ws = _store(tmp_path, _initiative(epic="e"), {"e": _epic("e", "bad name")})
    with pytest.raises(cli._TargetBranchRefusal, match=r"e\.md.*'bad name'.*initiative x"):
        cli._chair_gather(ws)(object(), datetime.now(UTC))
    branch, refusal = cli._initiative_target_branch({}, None, ws, "x")
    assert branch is None and "e.md" in refusal and "'bad name'" in refusal


_SIBLINGS = {"a": _epic("a", "release"), "b": "---\nid: other\n---\n", "c": _epic("c", "bad name")}


def test_a_broken_sibling_epic_file_does_not_change_where_another_epic_lands(tmp_path, monkeypatch, capsys):
    seen = _gather_args(monkeypatch)
    ws = _store(tmp_path, _initiative(epic="a"), _SIBLINGS)
    assert cli._initiative_target_branch({}, None, ws, "x") == ("release", None)
    cli._chair_gather(ws)(object(), datetime.now(UTC))
    assert seen["defs"] == {"a": {"id": "a", "title": "T", "target_branch": "release"}}
    capsys.readouterr()


def test_the_chair_refuses_only_when_an_initiative_names_the_epic_with_the_bad_branch(tmp_path, monkeypatch, capsys):
    seen = _gather_args(monkeypatch)
    cli._chair_gather(_store(tmp_path, _initiative(), _SIBLINGS))(object(), datetime.now(UTC))
    assert sorted(seen["defs"]) == ["a"] and "no initiative names that epic" in capsys.readouterr().err
    with pytest.raises(cli._TargetBranchRefusal, match=r"c\.md.*'bad name'"):
        cli._chair_gather(_store(tmp_path / "c", _initiative(epic="c"), _SIBLINGS))(object(), datetime.now(UTC))


def test_a_bad_branch_in_an_unrelated_epic_file_refuses_only_the_initiatives_that_name_it(tmp_path):
    assert cli._initiative_target_branch({}, None, _store(tmp_path, _initiative(), _SIBLINGS), "x") == (None, None)
    refused = cli._initiative_target_branch({}, None, _store(tmp_path / "c", _initiative(epic="c"), _SIBLINGS), "x")
    assert refused[0] is None and "c.md" in refused[1] and "'bad name'" in refused[1]


def test_a_real_cartridge_with_a_landing_target_branch_reaches_the_resolver(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", _REAL_TEAM_CARTRIDGE)
    (tmp_path / "cartridges" / "acme").mkdir(parents=True)
    (tmp_path / "cartridges" / "acme" / "cartridge.yaml").write_text("team: acme\nlanding_areas:\n  target_branch: next\n")
    profile = {"team": "acme", "cartridges_dir": str(tmp_path / "cartridges")}
    assert cli._initiative_target_branch(profile, None, _store(tmp_path), "x") == ("next", None)
    refused = cli._initiative_target_branch({**profile, "team": "ghost"}, None, _store(tmp_path / "g"), "x")
    assert refused[0] is None and "cartridge ghost cannot be resolved" in refused[1]


def test_a_repo_overlay_with_a_base_key_still_resolves_and_its_landing_target_branch_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", _REAL_TEAM_CARTRIDGE)
    (tmp_path / "cartridges" / "acme").mkdir(parents=True)
    (tmp_path / "cartridges" / "acme" / "cartridge.yaml").write_text("team: acme\n")
    repo = tmp_path / "repo"
    (repo / ".agent").mkdir(parents=True)
    (repo / ".agent" / "cartridge.yaml").write_text("base: develop\nlanding_areas:\n  target_branch: from-overlay\n")
    profile = {"team": "acme", "cartridges_dir": str(tmp_path / "cartridges")}
    branch, refusal = cli._initiative_target_branch(profile, repo, _store(tmp_path), "x")
    assert (branch, refusal) == ("from-overlay", None)


def test_land_initiative_names_the_phase_initiative_and_the_task_records_initiative(tmp_path):
    ws = _store(tmp_path)
    (ws / "work" / "x" / "seams").mkdir()
    (ws / "work" / "x" / "seams" / "t.md").write_text("---\nid: t\n---\n")
    assert cli._land_initiative(ws / "runs", "x-5", "seams", None) == "x"
    task_dir = ws / "runs" / "x-5" / "tasks" / "seams"
    task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record(run="x-5", initiative="x")), encoding="utf-8")
    assert cli._land_initiative(ws / "runs", "x-5", None, "seams-task") == "x"


# --- land ----------------------------------------------------------------------------------------------------------


def _land(tmp_path, monkeypatch, ws, *extra):
    """`cox runs land` (a dry run) of task record x-5, stopped at the plan; returns rc and the default branch the plan got."""
    task_dir = ws / "runs" / "x-5" / "tasks" / "seams"
    task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record(run="x-5", initiative="x")), encoding="utf-8")
    repo = tmp_path / "repo"
    _init_repo(repo)
    planned = []

    def stop(record, branches, default_branch, *args, **kwargs):
        planned.append(default_branch)
        raise _Stop

    monkeypatch.setattr(cli.land, "land_plan", stop)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\n")
    argv = ["runs", "land", "x-5", "--repo", str(repo), "--runs-dir", str(ws / "runs"), "--profile", str(profile), *extra]
    try:
        rc = cli.main(argv)
    except _Stop:
        rc = None
    return rc, planned


def test_land_without_a_target_branch_keeps_the_repo_default(tmp_path, monkeypatch):
    _, planned = _land(tmp_path, monkeypatch, _store(tmp_path))
    assert planned == [base_branch.base_name(str(tmp_path / "repo"))]


def test_land_cuts_from_the_initiatives_target_branch(tmp_path, monkeypatch):
    _, planned = _land(tmp_path, monkeypatch, _store(tmp_path, _initiative(target_branch="release")))
    assert planned == ["release"]


def test_land_takes_the_epics_target_branch(tmp_path, monkeypatch):
    _, planned = _land(tmp_path, monkeypatch, _store(tmp_path, _initiative(epic="e"), {"e": _epic("e", "epic-line")}))
    assert planned == ["epic-line"]


def test_land_reaches_the_cartridge_target_branch_through_the_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None, overlay_keys=None: ({"landing_areas": {"target_branch": "next"}}, None))
    _, planned = _land(tmp_path, monkeypatch, _store(tmp_path))
    assert planned == ["next"]


def test_land_with_the_base_flag_ignores_the_target_branch(tmp_path, monkeypatch):
    _, planned = _land(tmp_path, monkeypatch, _store(tmp_path, _initiative(target_branch="release")), "--base", "flagged")
    assert planned == ["flagged"]


def test_land_refuses_an_unresolvable_cartridge(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None, overlay_keys=None: ({}, "cartridge acme cannot be resolved: boom"))
    rc, planned = _land(tmp_path, monkeypatch, _store(tmp_path))
    assert (rc, planned) == (2, [])
    assert "land: refusing, cartridge acme cannot be resolved: boom" in capsys.readouterr().out


def test_land_refuses_an_epic_file_with_a_bad_branch_name(tmp_path, monkeypatch, capsys):
    rc, planned = _land(tmp_path, monkeypatch, _store(tmp_path, _initiative(epic="e"), {"e": _epic("e", "bad name")}))
    assert (rc, planned) == (2, [])
    out = capsys.readouterr().out
    assert "land: refusing" in out and "e.md" in out and "'bad name'" in out


def test_land_uses_the_valid_epic_branch_beside_broken_sibling_epic_files(tmp_path, monkeypatch):
    _, planned = _land(tmp_path, monkeypatch, _store(tmp_path, _initiative(epic="a"), _SIBLINGS))
    assert planned == ["release"]


def test_land_is_not_refused_by_a_bad_branch_in_an_epic_the_initiative_does_not_name(tmp_path, monkeypatch):
    _, planned = _land(tmp_path, monkeypatch, _store(tmp_path, _initiative(), _SIBLINGS))
    assert planned == [base_branch.base_name(str(tmp_path / "repo"))]


# --- launch --------------------------------------------------------------------------------------------------------


def _launch(tmp_path, monkeypatch, ws, *extra):
    """`cox route launch epic --dry-run` against `ws`; returns rc and the `base` the launch asked for (None when none)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    ws_text = (ws / "work" / "x" / "initiative.md").read_text().replace("repo: R", f"repo: {repo}")
    (ws / "work" / "x" / "initiative.md").write_text(ws_text)
    profile = _write_launch_profile(tmp_path, _write_harness(tmp_path), ws)
    asked = []
    real = base_branch.base_ref
    monkeypatch.setattr(base_branch, "base_ref", lambda repo_, flag=None, *a, **k: (asked.append(flag), real(repo_, flag, *a, **k))[1])
    rc = cli.main(["route", "launch", "epic", "--dry-run", "--profile", str(profile), "--initiative", str(ws / "work" / "x"), *extra])
    return rc, asked


def test_launch_without_a_target_branch_asks_for_no_base(tmp_path, monkeypatch):
    rc, asked = _launch(tmp_path, monkeypatch, _store(tmp_path))
    assert (rc, asked) == (0, [None])


def test_launch_cuts_from_the_initiatives_target_branch(tmp_path, monkeypatch):
    rc, asked = _launch(tmp_path, monkeypatch, _store(tmp_path, _initiative(target_branch="release")))
    assert (rc, asked) == (0, ["release"])


def test_launch_with_the_base_flag_ignores_the_target_branch(tmp_path, monkeypatch):
    rc, asked = _launch(tmp_path, monkeypatch, _store(tmp_path, _initiative(target_branch="release")), "--base", "flagged")
    assert (rc, asked) == (0, ["flagged"])


def test_launch_refuses_an_unresolvable_cartridge(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_team_cartridge", lambda profile, repo=None, overlay_keys=None: ({}, "cartridge acme cannot be resolved: boom"))
    rc, asked = _launch(tmp_path, monkeypatch, _store(tmp_path))
    assert (rc, asked) == (2, [])
    assert "routing: cartridge acme cannot be resolved: boom" in capsys.readouterr().out


def test_launch_refuses_an_epic_file_with_a_bad_branch_name(tmp_path, monkeypatch, capsys):
    rc, asked = _launch(tmp_path, monkeypatch, _store(tmp_path, _initiative(epic="e"), {"e": _epic("e", "bad name")}))
    assert (rc, asked) == (2, [])
    out = capsys.readouterr().out
    assert "routing:" in out and "e.md" in out and "'bad name'" in out


def test_launch_uses_the_valid_epic_branch_beside_broken_sibling_epic_files(tmp_path, monkeypatch):
    rc, asked = _launch(tmp_path, monkeypatch, _store(tmp_path, _initiative(epic="a"), _SIBLINGS))
    assert (rc, asked) == (0, ["release"])


def test_launch_is_not_refused_by_a_bad_branch_in_an_epic_the_initiative_does_not_name(tmp_path, monkeypatch):
    rc, asked = _launch(tmp_path, monkeypatch, _store(tmp_path, _initiative(), _SIBLINGS))
    assert (rc, asked) == (0, [None])


# --- help ----------------------------------------------------------------------------------------------------------


def test_the_launch_help_names_an_initiative_run_and_epic_still_parses(capsys):
    with pytest.raises(SystemExit):
        cli.main(["route", "launch", "--help"])
    out = capsys.readouterr().out
    assert "run of one" in out and "historical name" in out
    args = cli.build_parser().parse_args(["route", "launch", "epic", "--initiative", "x"])
    assert args.graph == "epic"
