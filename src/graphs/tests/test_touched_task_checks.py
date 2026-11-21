"""E1 finding 2: a phase-model task runs only cheap checks: the unit tests it touched, lint and the static checks, never
a whole component suite (three tasks each running all three suites on one machine timed out). The `# touched only`
mark, its scoping (`check_scope.select_checks`), and this repository's own task list on a real git tree. No mocks."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.check_scope import TOUCHED_ONLY, render_selection, select_checks, touched_tests
from harness.checks import NEW_MODEL_ONLY, for_level, old_model_checks, repo_checks
from harness.epic import Ctx
from harness.epic_build import _lane_checks

ROOT = Path(__file__).resolve().parents[3]
TOOLS, GRAPHS = "cd src/tools && pytest -q -n auto", "cd src/graphs && pytest -q -n auto"
CARTRIDGES = "cd src/cartridges && pytest -q -n auto"
RUFF = "python -m ruff check ."
STATIC = ["python .github/check_clean.py . --require-names", "python .github/check_unwired.py",
          "python .github/check_types.py"]
FILE = f"{TOOLS}  # touched only\n{GRAPHS}  # touched only\nlint\n# level: initiative\n{TOOLS}\n{GRAPHS}\n"
MAP = {
    "components": {"src/tools": frozenset({"src/graphs"}), "src/graphs": frozenset()},
    "test_reach": {
        "src/tools/tests/test_a.py": frozenset({"src/tools/agent_tools/a.py", "src/graphs/harness/b.py"}),
        "src/tools/tests/test_other.py": frozenset({"src/tools/agent_tools/other.py"}),
        "src/graphs/tests/test_b.py": frozenset({"src/graphs/harness/b.py"}),
        "src/graphs/tests/test_c.py": frozenset({"src/graphs/harness/c.py"}),
    },
    "unresolved": frozenset(),
}


def _cmds(checks) -> list[str]:
    return [str(c["cmd"]) for c in checks]


# -- the mark ---------------------------------------------------------------------------------------------------------

def test_a_touched_only_line_is_the_new_model_s_and_the_old_model_takes_the_suite_whole_from_the_widest_level():
    parsed = repo_checks(FILE)
    task = for_level(parsed, "task")
    assert task[0] == {"name": TOOLS, "cmd": TOOLS, NEW_MODEL_ONLY: True, TOUCHED_ONLY: True}
    assert _cmds(old_model_checks(parsed)) == ["lint", TOOLS, GRAPHS]  # each suite once, whole, as before levels


# -- the scoping ------------------------------------------------------------------------------------------------------

def test_touched_tests_are_the_changed_and_added_ones_and_those_reaching_a_changed_source():
    reach = MAP["test_reach"]
    assert touched_tests("src/tools", ["src/graphs/harness/b.py"], [], reach) == ["tests/test_a.py"]
    assert touched_tests("src/graphs", ["src/graphs/tests/test_c.py"], [], reach) == ["tests/test_c.py"]
    assert touched_tests("src/graphs", ["src/graphs/tests/test_new.py"], ["src/graphs/tests/test_new.py"], reach) == [
        "tests/test_new.py"]
    assert touched_tests("src/tools", ["README.md", "src/tools/tests/gone_test_x.txt"], [], reach) == []


def test_a_touched_only_check_is_narrowed_or_dropped_and_never_run_whole():
    checks = repo_checks(FILE)[:3]
    scoped = select_checks(checks, ["src/graphs/harness/b.py"], [], MAP)
    # The change reaches a tools test across the component line: that file runs, not the tools suite the plain rule
    # would run whole for importing graphs; the lint line passes through, in its place.
    assert _cmds(scoped.checks) == [f"{TOOLS} tests/test_a.py", f"{GRAPHS} tests/test_b.py", "lint"]
    assert scoped.selection["touched"] == {"src/tools": ["tests/test_a.py"], "src/graphs": ["tests/test_b.py"]}
    assert "touched only: src/tools (1 touched test file)" in render_selection(scoped.selection)
    # What the plain rule runs whole (an empty patch, a non-Python file, an unmapped source) runs nothing here.
    for changed in ([], ["docs/x.md"], ["src/graphs/harness/unmapped.py"]):
        dropped = select_checks(checks, changed, [], MAP)
        assert _cmds(dropped.checks) == ["lint"], changed
        assert dropped.selection["touched_skipped"] == ["src/tools", "src/graphs"]
    # A plain pytest line beside them keeps its own rule: whole for the empty patch.
    plain = [*checks, {"name": "unit", "cmd": "cd src/graphs && pytest -q"}]
    assert _cmds(select_checks(plain, [], [], MAP).checks) == ["lint", "cd src/graphs && pytest -q"]


# -- this repository, on a real tree ----------------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
                   check=True, capture_output=True)


def _write(repo: Path, path: str, text: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text, encoding="utf-8")


def _tree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write(repo, "src/tools/agent_tools/__init__.py", "")
    _write(repo, "src/tools/agent_tools/a.py", "def a():\n    return 1\n")
    _write(repo, "src/tools/tests/test_a.py", "from agent_tools.a import a\n\n\ndef test_a():\n    assert a() == 1\n")
    _write(repo, "src/tools/tests/test_z.py", "def test_z():\n    assert True\n")
    _write(repo, "src/graphs/harness/__init__.py", "")
    _write(repo, "src/graphs/harness/b.py", "B = 2\n")
    _write(repo, "src/graphs/tests/test_b.py", "from harness.b import B\n\n\ndef test_b():\n    assert B == 2\n")
    _write(repo, "src/cartridges/core/__init__.py", "")
    _write(repo, "src/cartridges/tests/test_c.py", "def test_c():\n    assert True\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _new_model_checks(tmp_path: Path) -> list:
    parsed = repo_checks((ROOT / ".agent-checks").read_text(encoding="utf-8-sig"))
    ctx = Ctx(repo=tmp_path, cartridge={"landing_areas": {"land_checks": "ci"}}, runner=None, specs={}, run_id="r1",
              date="2026-11-21", max_parallel=1, ledger_path=tmp_path / "ledger", provider_profile="p",
              runs_dir=tmp_path / "runs", worktree_root=tmp_path / "wt", assume=None, fix_attempts=None,
              initiative_id="i", default_ref="main", repo_checks=parsed, check_levels=True)
    return list(ctx.checks)


def test_this_repository_s_new_model_task_runs_the_cheap_set_on_a_real_tree(tmp_path):
    checks = _new_model_checks(tmp_path)
    assert _cmds(checks) == [TOOLS, GRAPHS, CARTRIDGES, RUFF, *STATIC]
    assert all(c.get(TOUCHED_ONLY) for c in checks[:3]) and not any(c.get(TOUCHED_ONLY) for c in checks[3:])
    repo = _tree(tmp_path)
    _write(repo, "src/tools/agent_tools/a.py", "def a():\n    return 1  # kept\n")
    _git(repo, "commit", "-qam", "a task that edits a")
    scoped, _ = _lane_checks(repo, "HEAD~1", checks)
    assert _cmds(scoped) == [f"{TOOLS} tests/test_a.py", RUFF, *STATIC]  # one test file, lint, the static checks

    _write(repo, "README.md", "notes\n")  # a change no test reaches: no suite at all, where the old rule ran it whole
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a docs task")
    assert _cmds(_lane_checks(repo, "HEAD~1", checks)[0]) == [RUFF, *STATIC]
    # A diff that cannot be read fails open for every check but a touched-only one, which is never run whole.
    assert _cmds(_lane_checks(repo, "no-such-ref", checks)[0]) == [RUFF, *STATIC]
