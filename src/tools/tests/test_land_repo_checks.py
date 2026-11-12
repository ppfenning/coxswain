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
    want = [(line, line) for line in ("cargo fmt --check", "cargo clippy --all-targets -- -D warnings", "cargo test")]
    assert _checks_of(tmp_path) == want
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert _checks_of(tmp_path) == want


def test_a_python_project_without_agent_checks_keeps_venv_then_uv_then_bare_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert _checks_of(tmp_path) == [("tests", "pytest -q")]
    (tmp_path / "uv.lock").write_text("")
    assert _checks_of(tmp_path) == [("tests", "uv run pytest -q")]
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert _checks_of(tmp_path) == [("tests", ".venv/bin/python -m pytest -q")]


@pytest.mark.parametrize("project_file", land.PYTHON_PROJECT_FILES)
def test_each_python_project_file_keeps_pytest(tmp_path, project_file):
    (tmp_path / project_file).write_text("")
    assert _checks_of(tmp_path) == [("tests", "pytest -q")]


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
    assert '"cargo test"' in captured.out


def test_the_dry_run_names_a_check_whose_name_is_not_its_program():
    steps = [{"kind": "checks", "checks": [("tests", "pytest -q")], "worktree_of": "pr/a"},
             {"kind": "checks", "checks": [("cargo", "cargo test")], "branch": "epic/x/p"}]
    assert cli._land_checks_lines(steps) == ["checks (pr/a): tests = pytest -q", "checks (epic/x/p): cargo test"]


def test_the_dry_run_prints_a_shell_line_as_written():
    steps = [{"kind": "checks", "checks": [("cd", "cd src/tools && pytest -q -n auto")], "worktree_of": "pr/a"}]
    assert cli._land_checks_lines(steps) == ["checks (pr/a): cd src/tools && pytest -q -n auto"]


def test_a_shell_line_is_run_by_a_shell_so_cd_and_true_lands_and_cd_and_false_refuses_by_name(tmp_path):
    (tmp_path / "sub").mkdir()
    assert cli._run_checks([("cd", "cd sub && true")], tmp_path) == (True, "1 checks passed")
    ok, detail = cli._run_checks([("cd", "cd sub && false")], tmp_path)
    assert not ok and detail.startswith("cd:")


def test_cd_sub_and_pytest_with_no_tests_in_sub_is_a_skip(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "pytest").write_text("#!/bin/sh\nexit 5\n")
    (tmp_path / "sub" / "pytest").chmod(0o755)
    ok, detail = cli._run_checks([("cd", "cd sub && PATH=.:$PATH pytest -q")], tmp_path)
    assert ok and "skipped" in detail


def test_the_real_agent_checks_plan_three_checks_each_in_its_subdirectory():
    root = Path(__file__).resolve().parents[3]
    lines = [c for _, c in _checks_of(root)]
    for sub in ("tools", "graphs", "cartridges"):
        assert f"cd src/{sub} && pytest -q -n auto" in lines
        assert (root / "src" / sub).is_dir()
    assert len([c for c in lines if c.startswith("cd src/")]) == 3


def test_the_worktree_venv_bin_is_first_on_path_and_without_a_venv_path_is_unchanged(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    for d, tag in ((other, "other"), ((tmp_path / "wt" / ".venv" / "bin"), "stub")):
        d.mkdir(parents=True, exist_ok=True)
        (d / "pytest").write_text(f"#!/bin/sh\necho {tag} >> {tmp_path / 'log'}\n")
        (d / "pytest").chmod(0o755)
    monkeypatch.setenv("PATH", f"{other}:{os.environ['PATH']}")
    assert cli._run_checks([("tests", "pytest -q")], tmp_path / "wt")[0]
    assert (tmp_path / "log").read_text() == "stub\n"
    (tmp_path / "wt2").mkdir()
    assert cli._checks_env(tmp_path / "wt2") is None
    assert cli._run_checks([("tests", "pytest -q")], tmp_path / "wt2")[0]
    assert (tmp_path / "log").read_text() == "stub\nother\n"


def test_the_dry_run_with_a_broken_cartridge_refuses_with_the_resolvers_message(tmp_path, capsys):
    """The target branch can live in the cartridge, so a land that cannot read it would plan from the wrong base."""
    repo = _task_repo(tmp_path)
    rc = _land(tmp_path, repo, profile=_profile(tmp_path, "no-such-team"))
    out = capsys.readouterr().out
    assert rc == 2
    assert "land: refusing, cartridge no-such-team cannot be resolved: " in out


def test_apply_refuses_before_any_step_when_the_cartridge_cannot_be_resolved(tmp_path, capsys, monkeypatch):
    repo = _task_repo(tmp_path)
    ran = []
    monkeypatch.setattr(cli, "_execute_land_step", lambda _repo, step, _forge=None: ran.append(step["kind"]) or (True, ""))
    rc = _land(tmp_path, repo, "--apply", profile=_profile(tmp_path, "no-such-team"))
    out = capsys.readouterr().out
    assert rc == 2
    assert "land: refusing, cartridge no-such-team cannot be resolved: " in out
    assert ran == []
    assert _git(repo, "branch", "--list", "pr/*") == ""


class _Forge:
    """A forge stub that records every required-checks read."""

    def __init__(self, required):
        self.required, self.reads = required, []

    def read_required_for_base(self, repo, base):
        self.reads.append((repo, base))
        if isinstance(self.required, Exception):
            raise self.required
        return self.required


def _plan_steps(facts):
    return land.land_plan(_record(), {"agents/epic-x-5/seams-task": [_TASK]}, "main", facts)


def _ci_facts(tmp_path, monkeypatch, forge, land_checks="ci", base="main"):
    monkeypatch.setattr(cli, "_cartridge_landing", lambda repo, profile: ({"land_checks": land_checks, "checks": []}, None))
    return cli._land_repo_facts(tmp_path, {}, base, forge)


def test_ci_with_a_required_check_plans_a_skipped_checks_step_and_the_executor_runs_nothing(tmp_path, monkeypatch):
    forge = _Forge(("test",))
    facts = _ci_facts(tmp_path, monkeypatch, forge)
    assert (facts["land_checks"], facts["required_checks"], forge.reads) == ("ci", ("test",), [(tmp_path, "main")])
    step = next(s for s in _plan_steps(facts) if s["kind"] == "checks")
    assert step["skipped"] == "CI runs the required status checks on main: test"

    def boom(*a, **k):
        raise AssertionError("a skipped checks step ran a command")
    monkeypatch.setattr(cli, "_run_checks", boom)
    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli._execute_land_step(tmp_path, step) == (True, f"checks: skipped ({step['skipped']})")


def test_ci_with_no_required_check_keeps_the_checks_and_the_dry_run_gives_the_reason(tmp_path, monkeypatch, capsys):
    facts = _ci_facts(tmp_path, monkeypatch, _Forge(()))
    step = next(s for s in _plan_steps(facts) if s["kind"] == "checks")
    assert step["kept"] == "checks kept locally: main has no required status check"
    assert cli._land_checks_lines([step])[-1] == "  checks kept locally: main has no required status check"
    ran = []
    monkeypatch.setattr(cli, "_run_checks", lambda checks, wt, *_: ran.append(checks) or (True, "ok"))
    assert cli._execute_land_step(tmp_path, step) == (True, "ok")
    assert ran == [step["checks"]]
    assert capsys.readouterr().out == "checks kept locally: main has no required status check\n"


@pytest.mark.parametrize("forge", [None, _Forge(RuntimeError("gh down"))])
def test_a_ci_forge_that_cannot_be_named_or_read_gives_required_checks_none(tmp_path, monkeypatch, forge):
    assert _ci_facts(tmp_path, monkeypatch, forge)["required_checks"] is None
    assert _ci_facts(tmp_path, monkeypatch, _Forge(("t",)), base=None)["required_checks"] is None


@pytest.mark.parametrize("land_checks", ["local", None])
def test_default_and_local_facts_plan_the_same_steps_and_make_no_forge_read(tmp_path, monkeypatch, land_checks):
    forge = _Forge(("test",))
    landing = {} if land_checks is None else {"land_checks": land_checks}
    monkeypatch.setattr(cli, "_cartridge_landing", lambda repo, profile: (landing, None))
    facts = cli._land_repo_facts(tmp_path, {}, "main", forge)
    assert forge.reads == [] and "required_checks" not in facts
    step = next(s for s in _plan_steps(facts) if s["kind"] == "checks")
    assert "skipped" not in step and "kept" not in step
    assert cli._land_checks_lines([step]) == [f"checks ({step['worktree_of']}): none declared"]


def _ci_overlay(repo):
    (repo / ".agent").mkdir()
    (repo / ".agent" / "cartridge.yaml").write_text("landing_areas:\n  land_checks: ci\n")


def test_land_checks_ci_in_the_repo_overlay_reaches_the_facts_through_the_real_cartridge(tmp_path):
    _ci_overlay(tmp_path)
    forge = _Forge(("test",))
    profile = {"team": "local", "cartridges_dir": str(_CARTRIDGES / "cartridges"), "skills_roots": [str(_CARTRIDGES / "skills-plugins")]}
    facts = cli._land_repo_facts(tmp_path, profile, "main", forge)
    assert (facts["land_checks"], facts["required_checks"], facts["cartridge_error"]) == ("ci", ("test",), None)


def test_a_failed_required_read_is_reported_on_stderr_with_its_cause(tmp_path, capsys):
    assert cli._required_checks(tmp_path, "main", _Forge(RuntimeError("gh down"))) is None
    assert "RuntimeError: gh down" in capsys.readouterr().err


def test_a_land_on_the_default_local_forge_never_reads_github_and_keeps_the_checks(tmp_path, monkeypatch, capsys):
    from agent_tools import forge_github

    reads = []
    monkeypatch.setattr(forge_github, "read_required_for_base", lambda repo, base: reads.append(base) or ("test",))
    repo = _task_repo(tmp_path)
    _ci_overlay(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "overlay")
    _land(tmp_path, repo, profile=_profile(tmp_path, "local"))
    err = capsys.readouterr().err
    assert reads == []
    assert "skipped" not in err and "cargo test" in err
    assert "checks kept locally: the required status checks on main could not be read" in err


def test_a_land_on_the_github_forge_reads_the_default_branch_and_skips_the_checks(tmp_path, monkeypatch, capsys):
    from agent_tools import forge_github

    reads = []
    monkeypatch.setattr(forge_github, "read_required_for_base", lambda repo, base: reads.append(base) or ("test",))
    repo = _task_repo(tmp_path)
    _ci_overlay(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "overlay")
    profile = _profile(tmp_path, "local")
    profile.write_text(profile.read_text() + "forge: github\n")
    _land(tmp_path, repo, profile=profile)
    err = capsys.readouterr().err
    assert reads == ["main"]
    assert "skipped (CI runs the required status checks on main: test)" in err


def test_the_dry_run_line_for_a_skipped_step_names_the_worktree_the_skip_and_its_reason():
    step = {"kind": "checks", "checks": [("tests", "pytest -q")], "worktree_of": "pr/a", "skipped": "CI runs the required status checks on main: test"}
    assert cli._land_checks_lines([step]) == ["checks (pr/a): skipped (CI runs the required status checks on main: test)"]


def test_a_ci_land_whose_poll_reports_no_checks_still_fails_at_wait_checks(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent_tools import forge_github

    real_wait = forge_github.wait_checks
    steps = _plan_steps(_ci_facts(tmp_path, monkeypatch, _Forge(("test",))))
    kinds = [s["kind"] for s in steps]
    assert "skipped" in steps[kinds.index("checks")] and kinds.index("checks") < kinds.index("wait_checks")
    monkeypatch.setattr(forge_github, "_run", lambda argv, *, timeout, cwd=None: SimpleNamespace(returncode=0, stdout="abc\n", stderr=""))
    monkeypatch.setattr(forge_github, "_read_required", lambda repo, ref: ())
    monkeypatch.setattr(forge_github, "_read_checks", lambda repo, ref="HEAD": (True, ({"check_runs": []}, {"statuses": []})))
    monkeypatch.setattr(forge_github, "read_check_suites", lambda repo, sha: ())
    clock = iter(range(0, 10_000, 30))
    wait = steps[kinds.index("wait_checks")]
    monkeypatch.setattr(forge_github, "wait_checks", lambda repo, timeout_s, ref="HEAD": real_wait(repo, timeout_s, sleep=lambda s: None, now=lambda: next(clock), ref=ref))
    assert cli._execute_land_step(tmp_path, wait, forge_github) == (False, "no checks reported within 180s")


_TICKET = [{"name": "e2e", "cmd": "cd e2e && pytest -q"}, {"name": "lint", "cmd": "ruff check ."}]
_BRANCH = "card/x/wire"
_APPROVED = {"task": "a", "title": "a", "repo": "wire", "status": "approved", "review": {"verdict": "approve"}}


def _card_plan(facts, **record):
    phase = {"run": "epic-x-5", "phase": "seams", "initiative": "x", "card_branch": _BRANCH, "last_phase": True, **record}
    return land.land_plan(phase, {}, "main", facts, items=[{"id": "a", "status": "approved"}], task_records=[_APPROVED],
                          repo="wire", ticket_tasks=("a",), ticket_present=())


def _kinds_of(steps):
    return [s["kind"] for s in steps]


def _ticket_steps(steps):
    return [s for s in steps if s.get("gate") == "ticket"]


def test_a_ticket_grain_plan_has_one_ticket_step_after_checks_and_before_push_and_pr_create():
    steps = _card_plan({"ticket_checks": _TICKET})
    [gate] = _ticket_steps(steps)
    assert gate == {"kind": "checks", "gate": "ticket", "checks": [("e2e", "cd e2e && pytest -q"), ("lint", "ruff check .")],
                    "worktree_of": _BRANCH}
    i = steps.index(gate)
    assert _kinds_of(steps)[i - 1] == "checks" and "gate" not in steps[i - 1]
    assert i < _kinds_of(steps).index("push") < _kinds_of(steps).index("pr_create")


def test_ticket_checks_run_in_the_land_that_opens_the_pr_not_in_every_phase_land():
    assert _ticket_steps(_card_plan({"ticket_checks": _TICKET}, last_phase=False)) == []


def test_a_task_grain_plan_has_no_ticket_step():
    assert _ticket_steps(_card_plan({"ticket_checks": _TICKET}, land_at="task")) == []


def test_a_repo_with_no_ticket_checks_plans_the_same_steps_as_before():
    plain = _card_plan({})
    assert _card_plan({"ticket_checks": []}) == plain == _card_plan({"ticket_checks": None})
    assert land.ticket_checks_steps({}, "b") == [] and _ticket_steps(plain) == []


def test_the_phase_plan_that_opens_the_ticket_pr_has_the_ticket_step_once_before_push():
    phase = {"run": "epic-x-5", "phase": "seams", "initiative": "x", "last_phase": True}
    task = {**_APPROVED, "arbitration": {"verdict": "approve"}}

    def plan(facts, **over):
        return land.land_plan({**phase, **over}, {"epic/x/seams": ["c1"]}, "main", facts,
                              items=[{"id": "a", "status": "approved"}], task_records=[task],
                              repo="wire", ticket_tasks=("a",), ticket_present=())
    steps = plan({"ticket_checks": _TICKET})
    [gate] = _ticket_steps(steps)
    assert _kinds_of(steps).index("checks") < steps.index(gate) < _kinds_of(steps).index("push")
    assert _ticket_steps(plan({"ticket_checks": _TICKET}, land_at="task")) == []
    assert plan({}) == plan({"ticket_checks": []})


def test_the_land_facts_read_ticket_checks_from_the_cartridge_alone(tmp_path, monkeypatch):
    (tmp_path / ".agent-checks").write_text("echo task\n")
    monkeypatch.setattr(cli, "_cartridge_landing", lambda repo, profile: ({"ticket_checks": _TICKET}, None))
    facts = cli._land_repo_facts(tmp_path, {}, "main")
    assert facts["ticket_checks"] == _TICKET
    assert all(c["cmd"] != _TICKET[0]["cmd"] for c in facts["checks"])
    monkeypatch.setattr(cli, "_cartridge_landing", lambda repo, profile: ({}, None))
    assert cli._land_repo_facts(tmp_path, {}, "main")["ticket_checks"] == []


def test_the_dry_run_labels_the_ticket_gate_step():
    [gate] = _ticket_steps(_card_plan({"ticket_checks": _TICKET}))
    assert cli._land_checks_lines([gate]) == [f"ticket gate ({_BRANCH}): e2e = cd e2e && pytest -q; lint = ruff check ."]


def test_the_runner_runs_a_ticket_step_under_the_repo_lock_and_an_ordinary_step_without_it(tmp_path, monkeypatch):
    from harness import check_gate
    events = []

    class FakeLock:
        def __init__(self, repo):
            events.append(("lock", repo))

        def __enter__(self):
            events.append("enter")

        def __exit__(self, *exc):
            events.append("exit")
    monkeypatch.setattr(check_gate, "repo_gate_lock", FakeLock)
    monkeypatch.setattr(cli, "_run_checks", lambda checks, wt, *_: events.append(("run", checks)) or (True, "ok"))
    [gate] = _ticket_steps(_card_plan({"ticket_checks": _TICKET}))
    assert cli._execute_land_step(tmp_path, gate) == (True, "ok")
    assert events == [("lock", tmp_path), "enter", ("run", gate["checks"]), "exit"]
    events.clear()
    ordinary = {"kind": "checks", "checks": [("t", "true")], "worktree_of": _BRANCH}
    assert cli._execute_land_step(tmp_path, ordinary) == (True, "ok")
    assert events == [("run", ordinary["checks"])]


def test_a_failing_ticket_check_fails_the_step_naming_the_check(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    monkeypatch.setattr(cli, "_land_worktree", lambda repo, branch: tmp_path)
    step = land.ticket_checks_steps({"ticket_checks": [{"name": "e2e", "cmd": "false"}]}, _BRANCH)[0]
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert not ok and detail.startswith("e2e:")
