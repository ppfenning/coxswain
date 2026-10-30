from types import SimpleNamespace

import pytest

from agent_tools import cli, land

SKIP = "checks: tests skipped (pytest exit 5, no tests ran)"
FORGE = SimpleNamespace(push=lambda repo, branch: (True, f"pushed {branch}"))
# A shell function stands in for pytest: exit 5 is what pytest says when it collects nothing.
NO_TESTS = "pytest() { echo 'no tests ran in 0.00s'; return 5; }; pytest -q"
EXIT_1 = "pytest() { echo 'no tests ran in 0.00s'; return 1; }; pytest -q"


def _walk(tmp_path, checks):
    steps = [{"kind": "checks", "checks": checks}, {"kind": "push", "branch": "pr"}]
    return cli._land_walk(tmp_path, steps, steps, None, None, "merge", False, FORGE, [])


@pytest.mark.parametrize("line, code, verdict", [
    ("pytest -q", 0, "pass"),
    ("pytest -q", 5, "skip"),
    ("python3 -m pytest", 5, "skip"),
    ("uv run pytest", 5, "skip"),
    ("coverage run pytest", 5, "fail"),
    ("pytest -q", 1, "fail"),
    ("ruff check .", 5, "fail"),
    ("cd src/tools && pytest -q -n auto", 5, "skip"),
    ("cd sub && A=1 pytest -q", 5, "skip"),
    ("pytest -q; false", 5, "fail"),
    ("pytest -q || ruff check .", 5, "fail"),
    ("pytest 'unclosed", 5, "fail"),
])
def test_check_exit_rules(line, code, verdict):
    assert land.check_exit(line, code) == verdict


def test_last_command_is_the_words_after_the_last_separator_and_assignments():
    assert land.last_command("cd sub && pytest -q") == ["pytest", "-q"]
    assert land.last_command("A=1 B=2 pytest") == ["pytest"]
    assert land.last_command("pytest -q; true") == ["true"]


def test_pytest_exit_5_skips_names_the_skip_and_the_land_pushes(tmp_path, capsys):
    _, reached, _ = _walk(tmp_path, [("tests", NO_TESTS)])
    out = capsys.readouterr().out.splitlines()
    assert reached == ["checks", "push"]
    assert out[:3] == ["checks: 1 checks passed", SKIP, "push: pushed pr"]


def test_pytest_exit_1_refuses(tmp_path):
    ok, detail = cli._execute_land_step(tmp_path, {"kind": "checks", "checks": [("tests", EXIT_1)]})
    assert (ok, detail) == (False, "tests: no tests ran in 0.00s")


def test_non_pytest_exit_5_still_refuses(tmp_path):
    _, reached, _ = _walk(tmp_path, [("tests", "ruff() { return 5; }; ruff check .")])
    assert reached == ["checks"]


def test_a_skip_before_a_failure_is_still_named(tmp_path):
    ok, detail = cli._execute_land_step(tmp_path, {"kind": "checks", "checks": [("tests", NO_TESTS), ("lint", "echo bad; false")]})
    assert (ok, detail) == (False, f"lint: bad\n{SKIP}")
