"""`_run_checks` reuses a passing result for the same tree and command (`harness.check_reuse`), and names it."""

import json
import os
import subprocess as sp

import pytest

from agent_tools import cli, land
from harness import check_reuse

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
_REUSED = "reused: tests (same tree and command passed earlier)"


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_ENV).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "f").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _result(cmd, exit_code=0, outcome="passed", name="tests"):
    return {"name": name, "cmd": cmd, "passed": exit_code == 0, "outcome": outcome, "error": None, "exit_code": exit_code,
            "counts": {}, "output_tail": "boom" if exit_code else "", "excerpt": "", "cpu_seconds": 1.0}


class _Ran(list):
    """The commands the shared function actually ran; each exit code is taken from `codes` (default 0)."""

    def __init__(self):
        super().__init__()
        self.codes: dict[str, list[int]] = {}

    def __call__(self, _worktree, checks, **_kw):
        out = []
        for check in checks:
            self.append(check["cmd"])
            exit_code = (self.codes.get(check["cmd"]) or [0]).pop(0)
            out.append(_result(check["cmd"], exit_code, "passed" if exit_code == 0 else "failed", check["name"]))
        return out


@pytest.fixture
def ran(monkeypatch):
    fake = _Ran()
    monkeypatch.setattr(check_reuse, "run_checks", fake)
    return fake


def test_a_second_run_of_the_same_tree_and_command_reuses_the_pass_and_names_it(repo, ran):
    assert cli._run_checks([("tests", "pytest -q")], repo) == (True, "1 checks passed")
    ok, detail = cli._run_checks([("tests", "pytest -q")], repo)
    assert ok
    assert ran == ["pytest -q"]
    assert detail.splitlines() == ["1 checks passed", f"checks: {_REUSED}"]


def test_a_first_run_misses_runs_the_check_and_appends_the_pass_to_the_ledger(repo, ran):
    ok, detail = cli._run_checks([("tests", "pytest -q")], repo)
    assert (ok, detail, ran) == (True, "1 checks passed", ["pytest -q"])
    rows = [json.loads(line) for line in (repo / ".git" / check_reuse.LEDGER_NAME).read_text().splitlines()]
    assert [(row["name"], row["cmd"], row["passed"]) for row in rows] == [("tests", "pytest -q", True)]


def test_a_changed_command_on_the_same_tree_misses(repo, ran):
    cli._run_checks([("tests", "pytest -q")], repo)
    ok, detail = cli._run_checks([("tests", "pytest -x")], repo)
    assert ok
    assert ran == ["pytest -q", "pytest -x"]
    assert "reused" not in detail


def test_a_changed_tree_with_the_same_command_misses(repo, ran):
    cli._run_checks([("tests", "pytest -q")], repo)
    (repo / "f").write_text("y")
    cli._run_checks([("tests", "pytest -q")], repo)
    assert ran == ["pytest -q", "pytest -q"]


def test_a_failed_check_is_not_recorded_and_the_next_run_runs_it_again(repo, ran):
    ran.codes["pytest -q"] = [1, 0]
    ok, detail = cli._run_checks([("tests", "pytest -q")], repo)
    assert (ok, detail) == (False, "tests: boom")
    assert not (repo / ".git" / check_reuse.LEDGER_NAME).exists()
    ok, detail = cli._run_checks([("tests", "pytest -q")], repo)
    assert (ok, detail) == (True, "1 checks passed")
    assert ran == ["pytest -q", "pytest -q"]


def test_a_prior_failed_row_in_the_ledger_does_not_count(repo, ran):
    tree = check_reuse.tree_of(repo)
    row = {**check_reuse.ledger_row(_result("pytest -q", 1, "failed"), tree)}
    (repo / ".git" / check_reuse.LEDGER_NAME).write_text(json.dumps(row) + "\n")
    ok, detail = cli._run_checks([("tests", "pytest -q")], repo)
    assert (ok, detail) == (True, "1 checks passed")
    assert ran == ["pytest -q"]


def test_the_ticket_gate_shape_of_a_line_named_by_itself_runs_once_then_is_reused(repo, ran):
    checks = [("pytest -q", "pytest -q"), ("ruff check", "ruff check")]
    assert cli._run_checks(checks, repo) == (True, "2 checks passed")
    ok, detail = cli._run_checks(checks, repo)
    assert ok
    assert ran == ["pytest -q", "ruff check"]
    assert detail.splitlines()[1:] == [
        "checks: reused: pytest -q (same tree and command passed earlier)",
        "checks: reused: ruff check (same tree and command passed earlier)",
    ]


def test_the_pytest_no_tests_skip_is_still_a_skip_and_is_never_reused(repo, monkeypatch):
    """The pytest no-tests skip is outcome `skipped`, so it is never written to the ledger and never reused."""
    skip = {**_result("pytest -q", 5, "skipped"), "passed": True}
    runs = []
    monkeypatch.setattr(check_reuse, "run_checks", lambda _wt, checks, **_kw: runs.append(1) or [skip])
    for _ in range(2):
        ok, detail = cli._run_checks([("tests", "pytest -q")], repo)
        assert ok
        assert detail.splitlines() == ["1 checks passed", land.skip_line("tests")]
    assert len(runs) == 2


def test_the_git_common_dir_is_the_dot_git_of_a_repo_and_none_outside_one(repo, tmp_path):
    assert cli._git_common_dir(repo) == (repo / ".git").resolve()
    assert cli._git_common_dir(tmp_path / "nowhere") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli._git_common_dir(plain) is None


def test_checks_detail_lists_skipped_then_reused_then_flaky():
    assert land.checks_detail("2 checks passed", ["a"], ["flaky note"], ["b"]).splitlines() == [
        "2 checks passed",
        land.skip_line("a"),
        "checks: reused: b (same tree and command passed earlier)",
        "checks: flaky note",
    ]
