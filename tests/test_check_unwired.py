"""The unwired check (H10 item 5): its pure core on example inputs, then the script against a real git repository."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "check_unwired.py"
_SPEC = importlib.util.spec_from_file_location("check_unwired", SCRIPT)
check_unwired = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_unwired)

Change = check_unwired.Change
Symbol = check_unwired.Symbol

CORE = "src/pkg/core.py"
CLI = "src/pkg/cli.py"


def test_name_status_reads_one_path_per_change_and_two_for_a_rename():
    text = "M\0src/a.py\0A\0src/b.py\0R087\0src/old.py\0src/new.py\0D\0src/gone.py\0"
    assert check_unwired.parse_name_status(text) == (
        Change("M", "src/a.py", "src/a.py"),
        Change("A", None, "src/b.py"),
        Change("R", "src/old.py", "src/new.py"),
        Change("D", "src/gone.py", "src/gone.py"),
    )


def test_an_empty_diff_has_no_changes():
    assert check_unwired.parse_name_status("") == ()


@pytest.mark.parametrize("path", [
    "src/tools/tests/test_x.py", "src/tools/tests/helper.py", "smoke/plan.py", "src/a/conftest.py",
    "src/a/test_b.py", "src/a/b_test.py", "src/a/fixtures/sample.py", "tests/test_check_unwired.py",
])
def test_test_paths(path):
    assert check_unwired.is_test_path(path)


@pytest.mark.parametrize("path", ["src/tools/agent_tools/cli.py", ".github/check_clean.py", "src/a/testing_mode.py"])
def test_non_test_paths(path):
    assert not check_unwired.is_test_path(path)


def test_scope_is_non_test_python_under_src():
    assert check_unwired.in_scope("src/pkg/core.py")
    assert not check_unwired.in_scope("src/pkg/tests/test_core.py")
    assert not check_unwired.in_scope(".github/check_unwired.py")
    assert not check_unwired.in_scope("src/pkg/notes.md")


def test_public_symbols_are_top_level_defs_and_classes_without_a_leading_underscore():
    source = (
        "def run():\n    def inner(): ...\nasync def fetch(): ...\nclass Plan:\n    def method(self): ...\n"
        "def _private(): ...\nVALUE = 1\n"
    )
    assert check_unwired.public_symbols(CORE, source) == (
        Symbol(CORE, "run", "function", 1),
        Symbol(CORE, "fetch", "function", 3),
        Symbol(CORE, "Plan", "class", 4),
    )


def test_a_modified_file_adds_only_the_symbols_its_old_source_lacked():
    changes = (Change("M", CORE, CORE),)
    old = {CORE: "def kept(): ...\n"}
    new = {CORE: "def kept(): ...\ndef helper(): ...\n"}
    assert check_unwired.added_symbols(changes, old, new) == (Symbol(CORE, "helper", "function", 2),)


def test_a_new_file_adds_its_module_and_its_symbols():
    changes = (Change("A", None, "src/pkg/window.py"),)
    new = {"src/pkg/window.py": "def parse(): ...\n"}
    assert check_unwired.added_symbols(changes, {}, new) == (
        Symbol("src/pkg/window.py", "window", "module", 1),
        Symbol("src/pkg/window.py", "parse", "function", 1),
    )


def test_a_rename_compares_against_the_old_path():
    changes = (Change("R", "src/pkg/old.py", "src/pkg/new.py"),)
    old = {"src/pkg/old.py": "def kept(): ...\n"}
    new = {"src/pkg/new.py": "def kept(): ...\n"}
    assert check_unwired.added_symbols(changes, old, new) == ()


def test_tests_deletions_and_private_modules_add_nothing():
    changes = (
        Change("A", None, "src/pkg/tests/test_core.py"),
        Change("D", CORE, CORE),
        Change("A", None, "src/pkg/_impl.py"),
    )
    new = {"src/pkg/tests/test_core.py": "def test_x(): ...\n", "src/pkg/_impl.py": ""}
    assert check_unwired.added_symbols(changes, {}, new) == ()


def test_python_references_cover_names_attributes_imports_and_dotted_strings():
    source = (
        "import pkg.window\n"
        "from pkg.core import helper\n"
        "value = core.build()\n"
        "run(plan)\n"
        "TABLE = {'go': 'pkg.handlers:on_go', 'note': 'not a path at all'}\n"
    )
    names = check_unwired.python_references(CLI, source)
    assert {"pkg", "window", "core", "helper", "build", "run", "plan", "handlers", "on_go"} <= names
    assert "value" not in names
    assert "path" not in names


def test_a_def_does_not_reference_itself_when_skipped():
    source = "def helper(n):\n    return helper(n - 1)\n"
    assert "helper" not in check_unwired.python_references(CORE, source, skip_def="helper")
    assert "helper" in check_unwired.python_references(CORE, source)


def test_dunder_all_and_init_reexports_are_not_references():
    assert "helper" not in check_unwired.python_references(CORE, "__all__ = ['helper']\n")
    assert "helper" not in check_unwired.python_references("src/pkg/__init__.py", "from .core import helper\n")


def test_text_references_take_the_parts_of_dotted_paths():
    toml = '[project.scripts]\ncox = "agent_tools.cli:main"\n# a helper word in prose\n'
    names = check_unwired.text_references(toml)
    assert {"agent_tools", "cli", "main"} <= names
    assert "helper" not in names


def test_a_helper_only_a_test_calls_is_unwired():
    helper = Symbol(CORE, "helper", "function", 2)
    sources = {
        CORE: "def helper():\n    return helper()\n",
        "src/pkg/tests/test_core.py": "from pkg.core import helper\nhelper()\n",
    }
    assert check_unwired.unwired((helper,), sources, frozenset()) == (helper,)


def test_a_caller_in_another_module_or_the_same_module_wires_it():
    helper = Symbol(CORE, "helper", "function", 1)
    assert check_unwired.unwired((helper,), {CORE: "def helper(): ...\n", CLI: "from pkg.core import helper\n"},
                                 frozenset()) == ()
    assert check_unwired.unwired((helper,), {CORE: "def helper(): ...\ndef run():\n    helper()\n"},
                                 frozenset()) == ()


def test_a_new_module_needs_an_import_from_another_file():
    module = Symbol("src/pkg/window.py", "window", "module", 1)
    alone = {"src/pkg/window.py": "import window\n"}
    assert check_unwired.unwired((module,), alone, frozenset()) == (module,)
    imported = dict(alone, **{CLI: "from pkg import window\n"})
    assert check_unwired.unwired((module,), imported, frozenset()) == ()


def test_an_allowlisted_entry_point_passes():
    hook = Symbol(CORE, "on_start", "function", 1)
    assert check_unwired.unwired((hook,), {CORE: "def on_start(): ...\n"}, frozenset({(CORE, "on_start")})) == ()


def test_the_allowlist_requires_a_reason_and_an_existing_path():
    text = (
        "# entry points nothing calls by name\n"
        f"{CORE}::on_start  # plugin hook, called by the host by name\n"
        f"{CORE}::on_stop\n"
        "src/pkg/gone.py::old  # removed module\n"
        "no separator  # reason\n"
    )
    allow = check_unwired.parse_allowlist(text, [CORE])
    assert allow.entries == frozenset({(CORE, "on_start")})
    assert len(allow.errors) == 3
    assert "has no reason" in allow.errors[0]
    assert "does not exist" in allow.errors[1]
    assert "expected `path::name" in allow.errors[2]


def test_the_report_names_each_unwired_symbol_with_its_line():
    helper = Symbol(CORE, "helper", "function", 7)
    lines = check_unwired.report((helper,), (helper,))
    assert lines[0].startswith(f"{CORE}:7: unwired: function helper has no reference outside tests")
    assert check_unwired.report((helper,), ()) == (
        "unwired: 1 added public symbol(s) under src/, each referenced outside tests",
    )


# ── end to end: the script itself against a real repository ──────────────────

GIT_ENV = {
    "GIT_AUTHOR_NAME": "builder", "GIT_AUTHOR_EMAIL": "builder@example.com",
    "GIT_COMMITTER_NAME": "builder", "GIT_COMMITTER_EMAIL": "builder@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30,
                   env={**os.environ, **GIT_ENV})


def write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def check(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, CORE, "def parse(text):\n    return text.split()\n")
    write(tmp_path, CLI, "from pkg.core import parse\n\n\ndef main(argv):\n    return parse(argv[0])\n")
    write(tmp_path, "pyproject.toml", '[project.scripts]\ntool = "pkg.cli:main"\n')
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(tmp_path, "switch", "-q", "-c", "work")
    return tmp_path


def add_unwired_helper(repo: Path) -> None:
    write(repo, CORE, "def parse(text):\n    return text.split()\n\n\ndef count(text):\n    return len(parse(text))\n")
    write(repo, "src/pkg/tests/test_core.py", "from pkg.core import count\n\n\ndef test_count():\n"
                                              "    assert count('a b') == 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add count")


def test_end_to_end_an_unwired_helper_fails_and_wiring_it_passes(repo: Path):
    add_unwired_helper(repo)
    failed = check(repo)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert f"{CORE}:5: unwired: function count has no reference outside tests" in failed.stdout

    write(repo, CLI, "from pkg.core import count, parse\n\n\ndef main(argv):\n    return parse(argv[0]), count(argv[0])\n")
    git(repo, "commit", "-q", "-am", "wire count")
    passed = check(repo)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "1 added public symbol(s)" in passed.stdout


def test_end_to_end_uncommitted_and_untracked_work_is_checked(repo: Path):
    write(repo, "src/pkg/window.py", "def span(text):\n    return text\n")
    result = check(repo)
    assert result.returncode == 1
    assert "module window" in result.stdout and "function span" in result.stdout


def test_end_to_end_an_allowlisted_entry_point_passes(repo: Path):
    add_unwired_helper(repo)
    write(repo, ".github/unwired-allow.txt", f"{CORE}::count  # called by the host by name\n")
    assert check(repo).returncode == 0
    write(repo, ".github/unwired-allow.txt", f"{CORE}::count\n")
    refused = check(repo)
    assert refused.returncode == 2 and "has no reason" in refused.stderr


def test_end_to_end_an_unknown_base_cannot_run(repo: Path):
    result = check(repo, "--base", "no-such-ref")
    assert result.returncode == 2
    assert "cannot read the diff" in result.stderr


def test_end_to_end_the_base_branch_itself_adds_nothing(repo: Path):
    git(repo, "switch", "-q", "main")
    result = check(repo)
    assert result.returncode == 0
    assert "0 added public symbol(s)" in result.stdout
