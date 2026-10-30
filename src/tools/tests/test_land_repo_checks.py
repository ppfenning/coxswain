"""`cox runs land` runs the repo's own checks: `.agent-checks` first, the pytest default only for a Python project."""

import json
import os
import subprocess as sp
from pathlib import Path

import pytest
from test_land import _record

from agent_tools import cli, land

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
_TASK = "agents/epic-x-5/seams-task"
_CARGO = "cargo fmt --check\ncargo clippy --all-targets -- -D warnings\ncargo test\n"
_CARTRIDGES = Path(__file__).resolve().parents[2] / "cartridges"  # src/cartridges, the real `local` team cartridge


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_ENV).stdout.strip()


def _checks_of(repo, profile=None):
    facts = cli._land_repo_facts(repo, profile or {})
    plan = land.land_plan(_record(), {"agents/epic-x-5/seams-task": [_TASK]}, "main", facts)
    return next(s["checks"] for s in plan if s["kind"] == "checks")


def _profile(tmp_path, team):
    path = tmp_path / "profile.yaml"
    path.write_text(f"team: {team}\ncartridges_dir: {_CARTRIDGES / 'cartridges'}\nskills_roots: [{_CARTRIDGES / 'skills-plugins'}]\n"
                    f"provider_profile: {tmp_path}/p.yaml\nharness_dir: {tmp_path}\nworkspace_dir: {tmp_path}\nassume: a\n")
    return path


def _task_repo(tmp_path):
    """A clean repo whose `.agent-checks` is the dash's, one commit ahead on the task branch, with its run record."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x")
    (repo / ".agent-checks").write_text(_CARGO)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "checkout", "-qb", _TASK)
    (repo / "f").write_text("y")
    _git(repo, "commit", "-aqm", "Add seams module")
    _git(repo, "checkout", "-q", "main")
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"
    task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    (tmp_path / "runs/policy.tracker.json").write_text('{"tracker": "github-projects"}', encoding="utf-8")
    return repo


def _land(tmp_path, repo, *extra, profile=None):
    return cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs"),
                     *(["--profile", str(profile)] if profile else []), *extra])


def test_resolved_checks_puts_the_cartridge_first_and_drops_a_repeated_cmd():
    cartridge = [{"name": "lint", "cmd": "ruff check ."}]
    repo = [{"name": "ruff", "cmd": "ruff check ."}, {"name": "cargo", "cmd": "cargo test"}]
    assert land.resolved_checks(cartridge, repo) == [{"name": "lint", "cmd": "ruff check ."}, {"name": "cargo", "cmd": "cargo test"}]


def test_agent_checks_are_planned_and_a_stray_venv_changes_nothing(tmp_path):
    (tmp_path / ".agent-checks").write_text(_CARGO)
    want = [("cargo", ["cargo", "fmt", "--check"]), ("cargo", ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"]),
            ("cargo", ["cargo", "test"])]
    assert _checks_of(tmp_path) == want
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert _checks_of(tmp_path) == want


def test_a_python_project_without_agent_checks_keeps_venv_then_uv_then_bare_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert _checks_of(tmp_path) == [("tests", ["pytest", "-q"])]
    (tmp_path / "uv.lock").write_text("")
    assert _checks_of(tmp_path) == [("tests", ["uv", "run", "pytest", "-q"])]
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert _checks_of(tmp_path) == [("tests", [".venv/bin/python", "-m", "pytest", "-q"])]


@pytest.mark.parametrize("project_file", land.PYTHON_PROJECT_FILES)
def test_each_python_project_file_keeps_pytest(tmp_path, project_file):
    (tmp_path / project_file).write_text("")
    assert _checks_of(tmp_path) == [("tests", ["pytest", "-q"])]


def test_a_repo_with_neither_plans_no_checks_even_with_a_stray_venv(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert _checks_of(tmp_path) == []
    assert cli._run_checks([], tmp_path) == (True, "none declared")
    assert cli._land_checks_lines([{"kind": "checks", "checks": [], "worktree_of": "pr/x"}]) == ["checks (pr/x): none declared"]


def test_the_real_team_cartridge_checks_come_first_then_the_repo_checks(tmp_path):
    (tmp_path / ".agent-checks").write_text(_CARGO)
    facts = cli._land_repo_facts(tmp_path, {"team": "local", "cartridges_dir": str(_CARTRIDGES / "cartridges"),
                                            "skills_roots": [str(_CARTRIDGES / "skills-plugins")]})
    assert facts["cartridge_error"] is None
    assert [c["cmd"] for c in facts["checks"]] == ["ruff check .", "pytest -q", *_CARGO.splitlines()]


def test_an_unresolvable_cartridge_is_reported_and_leaves_the_repo_checks(tmp_path):
    (tmp_path / ".agent-checks").write_text(_CARGO)
    facts = cli._land_repo_facts(tmp_path, {"team": "no-such-team", "cartridges_dir": str(_CARTRIDGES / "cartridges"), "skills_roots": []})
    assert facts["cartridge_error"]
    assert [c["cmd"] for c in facts["checks"]] == _CARGO.splitlines()


def test_the_dry_run_lists_every_check_of_every_checks_step(tmp_path, capsys):
    repo = _task_repo(tmp_path)
    _land(tmp_path, repo)
    captured = capsys.readouterr()
    assert ("checks (pr/seams-task): cargo fmt --check; cargo clippy --all-targets -- -D warnings; cargo test") in captured.err
    assert '"cargo",\n          "test"' in captured.out


def test_the_dry_run_names_a_check_whose_name_is_not_its_program():
    steps = [{"kind": "checks", "checks": [("tests", ["pytest", "-q"])], "worktree_of": "pr/a"},
             {"kind": "checks", "checks": [("cargo", ["cargo", "test"])], "branch": "epic/x/p"}]
    assert cli._land_checks_lines(steps) == ["checks (pr/a): tests = pytest -q", "checks (epic/x/p): cargo test"]


def test_the_dry_run_with_a_broken_cartridge_warns_and_lists_the_short_list(tmp_path, capsys):
    repo = _task_repo(tmp_path)
    rc = _land(tmp_path, repo, profile=_profile(tmp_path, "no-such-team"))
    captured = capsys.readouterr()
    assert rc == 0
    assert "land: cartridge checks unavailable (" in captured.err
    assert "the list below is short" in captured.err
    assert "checks (pr/seams-task): cargo fmt --check;" in captured.err


def test_apply_refuses_before_any_step_when_the_cartridge_cannot_be_resolved(tmp_path, capsys, monkeypatch):
    repo = _task_repo(tmp_path)
    ran = []
    monkeypatch.setattr(cli, "_execute_land_step", lambda _repo, step, _forge=None: ran.append(step["kind"]) or (True, ""))
    rc = _land(tmp_path, repo, "--apply", profile=_profile(tmp_path, "no-such-team"))
    out = capsys.readouterr().out
    assert rc == 2
    assert "land: refusing, cartridge checks unavailable (" in out
    assert "the land would run fewer checks than the build did" in out
    assert ran == []
    assert _git(repo, "branch", "--list", "pr/*") == ""
