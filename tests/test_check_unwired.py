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
Ref = check_unwired.Ref

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


def names(refs):
    return {ref.name for ref in refs}


def test_python_refs_cover_names_attributes_imports_and_dotted_strings():
    source = (
        "import pkg.window\n"
        "from pkg.core import helper\n"
        "value = core.build()\n"
        "run(plan)\n"
        "TABLE = {'go': 'pkg.handlers:on_go', 'note': 'not a path at all'}\n"
    )
    found = names(check_unwired.python_refs(CLI, source))
    assert {"pkg", "window", "core", "helper", "build", "run", "plan", "handlers", "on_go"} <= found
    assert not {"value", "path", "go", "note"} & found


def test_a_bare_name_string_counts_only_as_the_name_getattr_or_hasattr_looks_up():
    keyed = "ROW = {'quarantine_blocks_dependents': True}\nfield('status')\n"
    assert not {"quarantine_blocks_dependents", "status"} & names(check_unwired.python_refs(CLI, keyed))
    looked_up = "handler = getattr(module, 'on_go')\nready = hasattr(module, 'on_stop')\n"
    assert {"on_go", "on_stop"} <= names(check_unwired.python_refs(CLI, looked_up))


def test_each_ref_carries_its_line_and_its_top_level_owner():
    source = "def run():\n    return helper()\n\n\nhelper()\n"
    refs = [ref for ref in check_unwired.python_refs(CORE, source) if ref.name == "helper"]
    assert sorted(refs, key=lambda ref: ref.line) == [Ref("helper", 2, "run", 2), Ref("helper", 5, None, 5)]


def test_a_use_spans_its_whole_statement_but_not_a_compound_body():
    source = "from pkg.a import (\n    bar,\n    foo,\n)\n\n\n@register\ndef run(\n    x,\n):\n    return foo(x)\n"
    spans = {(ref.name, ref.owner): ref.lines() for ref in check_unwired.python_refs(CLI, source)}
    assert spans[("foo", None)] == range(1, 5)  # the import's alias line 3, widened to the whole statement
    assert spans[("register", "run")] == range(7, 11)  # the decorator and header, not the body
    assert spans[("foo", "run")] == range(11, 12)  # the call in the body is its own statement


def test_a_multi_line_import_whose_first_line_changed_wires_every_name_in_it():
    sources = {CORE: "def helper(): ...\n", CLI: "from pkg.core import (\n    helper,\n)\n"}
    assert wired(sources, {CLI: frozenset({1})})


def test_a_def_the_same_diff_removes_from_another_file_was_moved_not_added():
    changes = (Change("M", "src/pkg/a.py", "src/pkg/a.py"), Change("A", None, "src/pkg/b.py"))
    old = {"src/pkg/a.py": "def foo(): ...\ndef bar(): ...\n"}
    new = {"src/pkg/a.py": "from pkg.b import foo\ndef bar(): ...\n", "src/pkg/b.py": "def foo(): ...\ndef baz(): ...\n"}
    assert check_unwired.removed_names(changes, old, new) == frozenset({"foo"})
    assert check_unwired.added_symbols(changes, old, new) == (
        Symbol("src/pkg/b.py", "b", "module", 1),
        Symbol("src/pkg/b.py", "baz", "function", 2),
    )


def test_a_deleted_files_defs_are_removed_names():
    changes = (Change("D", "src/pkg/old.py", "src/pkg/old.py"),)
    assert check_unwired.removed_names(changes, {"src/pkg/old.py": "def foo(): ...\n"}, {}) == frozenset({"foo"})


def test_an_init_imports_its_submodule_but_does_not_call_what_it_reexports():
    names_in_init = names(check_unwired.python_refs("src/pkg/__init__.py", "from .parts import helper\n"))
    assert "parts" in names_in_init and "helper" not in names_in_init


def test_dunder_all_and_init_reexports_are_not_references():
    assert "helper" not in names(check_unwired.python_refs(CORE, "__all__ = ['helper']\n"))
    assert "helper" not in names(check_unwired.python_refs("src/pkg/__init__.py", "from .core import helper\n"))


def test_text_refs_take_the_parts_of_dotted_paths_with_their_lines():
    toml = '[project.scripts]\ncox = "agent_tools.cli:main"\n# a helper word in prose\n'
    refs = check_unwired.text_refs(toml)
    assert {Ref("agent_tools", 2, None), Ref("cli", 2, None), Ref("main", 2, None)} <= set(refs)
    assert "helper" not in names(refs)


def test_added_lines_come_from_the_new_side_of_each_hunk():
    diff = (
        "diff --git a/src/pkg/cli.py b/src/pkg/cli.py\n--- a/src/pkg/cli.py\n+++ b/src/pkg/cli.py\n"
        "@@ -1 +1,2 @@\n-x\n+y\n+z\n@@ -9,0 +11 @@\n+w\n"
        "diff --git a/src/pkg/gone.py b/src/pkg/gone.py\n--- a/src/pkg/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        "diff --git a/src/pkg/cut.py b/src/pkg/cut.py\n--- a/src/pkg/cut.py\n+++ b/src/pkg/cut.py\n@@ -3 +2,0 @@\n-x\n"
    )
    assert check_unwired.parse_added_lines(diff) == {CLI: frozenset({1, 2, 11}), "src/pkg/cut.py": frozenset()}


def test_the_base_is_the_lanes_phase_branch_when_the_harness_exports_one():
    assert check_unwired.default_base({"COX_LANE_BASE": "epic/demo/p3-rollout"}) == "epic/demo/p3-rollout"
    assert check_unwired.default_base({"COX_LANE_BASE": ""}) == "origin/main"
    assert check_unwired.default_base({}) == "origin/main"


HELPER = Symbol(CORE, "helper", "function", 1)
NONE: frozenset = frozenset()


def wired(sources, added_lines, private=NONE, symbol=HELPER):
    return check_unwired.unwired((symbol,), sources, added_lines, private, NONE) == ()


def test_a_helper_only_a_test_calls_is_unwired():
    sources = {CORE: "def helper():\n    return helper()\n", "src/pkg/tests/test_core.py": "helper()\n"}
    assert not wired(sources, {"src/pkg/tests/test_core.py": frozenset({1})})


def test_a_caller_on_a_line_the_diff_adds_wires_it():
    sources = {CORE: "def helper(): ...\n", CLI: "import os\nfrom pkg.core import helper\n"}
    assert wired(sources, {CLI: frozenset({2})})


def test_a_use_on_a_line_the_diff_did_not_touch_is_a_coincidence():
    sources = {CORE: "def status(): ...\n", CLI: "status()\n"}
    assert not wired(sources, {CLI: NONE}, symbol=Symbol(CORE, "status", "function", 1))


def test_its_own_file_counts_on_any_line_outside_its_own_body():
    assert wired({CORE: "def helper(): ...\ndef run():\n    helper()\n"}, {})
    assert not wired({CORE: "def helper():\n    helper()\n"}, {})


def test_a_new_private_caller_counts_only_when_it_is_called():
    source = "def helper(): ...\ndef _wrap():\n    return helper()\n"
    private = frozenset({(CORE, "_wrap")})
    assert not wired({CORE: source}, {}, private)
    assert wired({CORE: source + "def run():\n    _wrap()\n"}, {}, private)
    looping = "def helper(): ...\ndef _a():\n    helper(); _b()\ndef _b():\n    _a()\n"
    assert not wired({CORE: looping}, {}, frozenset({(CORE, "_a"), (CORE, "_b")}))


def test_an_existing_private_caller_counts_as_any_caller():
    assert wired({CORE: "def helper(): ...\ndef _old():\n    helper()\n"}, {})


def test_a_new_module_needs_an_added_import_from_another_file():
    module = Symbol("src/pkg/window.py", "window", "module", 1)
    alone = {"src/pkg/window.py": "import window\n"}
    assert not wired(alone, {}, symbol=module)
    assert wired(dict(alone, **{CLI: "from pkg import window\n"}), {CLI: frozenset({1})}, symbol=module)


def test_an_allowlisted_entry_point_passes():
    hook = Symbol(CORE, "on_start", "function", 1)
    allow = frozenset({(CORE, "on_start")})
    assert check_unwired.unwired((hook,), {CORE: "def on_start(): ...\n"}, {}, NONE, allow) == ()


def test_the_allowlist_requires_a_reason_and_an_existing_path_and_name():
    text = (
        "# entry points nothing calls by name\n"
        f"{CORE}::on_start  # plugin hook, called by the host by name\n"
        f"{CORE}::on_stop\n"
        "src/pkg/gone.py::old  # removed module\n"
        f"{CORE}::renamed  # the hook was renamed\n"
        "no separator  # reason\n"
        f"{CORE}::core  # the module itself\n"
    )
    allow = check_unwired.parse_allowlist(text, {CORE: "def on_start(): ...\n"})
    assert allow.entries == frozenset({(CORE, "on_start"), (CORE, "core")})
    assert len(allow.errors) == 4
    assert "has no reason" in allow.errors[0]
    assert "does not exist" in allow.errors[1]
    assert "defines no renamed" in allow.errors[2]
    assert "expected `path::name" in allow.errors[3]


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


def check(repo: Path, *args: str, lane_base: str | None = None) -> subprocess.CompletedProcess:
    """The script as a lane runs it; the suite's own lane base, if it runs inside one, is never inherited."""
    env = {key: value for key, value in os.environ.items() if key != "COX_LANE_BASE"}
    env.update({"COX_LANE_BASE": lane_base} if lane_base else {})
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo), *args],
                          capture_output=True, text=True, timeout=60, env=env)


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


def test_end_to_end_a_lane_judges_its_own_diff_against_its_phase_branch(repo: Path):
    """A sibling task's unwired helper, already merged into the phase branch, is not this lane's finding."""
    git(repo, "switch", "-q", "-c", "epic/demo/p1", "main")
    git(repo, "switch", "-q", "-c", "sibling")
    add_unwired_helper(repo)
    git(repo, "switch", "-q", "epic/demo/p1")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge sibling", "sibling")
    git(repo, "switch", "-q", "-c", "task")
    write(repo, "src/pkg/span.py", "def width(text):\n    return len(text)\n")
    write(repo, CLI, "from pkg.core import parse\nfrom pkg.span import width\n\n\n"
                     "def main(argv):\n    return parse(argv[0]), width(argv[0])\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add and wire width")

    lane = check(repo, lane_base="epic/demo/p1")
    assert lane.returncode == 0, lane.stdout + lane.stderr
    assert "2 added public symbol(s)" in lane.stdout  # the span module and width, both wired

    whole = check(repo)
    assert whole.returncode == 1
    assert "function count" in whole.stdout and "width" not in whole.stdout

    flag = check(repo, "--base", "origin/main", lane_base="epic/demo/p1")
    assert flag.returncode == 1  # an explicit --base wins over the lane's


def test_end_to_end_a_private_wrapper_nothing_calls_does_not_wire_its_callee(repo: Path):
    write(repo, CORE, "def parse(text):\n    return text.split()\n\n\ndef count(text):\n    return len(parse(text))\n"
                      "\n\ndef _report(text):\n    return count(text)\n")
    assert check(repo).returncode == 1
    write(repo, CLI, "from pkg.core import _report, parse\n\n\ndef main(argv):\n    return parse(argv[0]), _report(argv[0])\n")
    assert check(repo).returncode == 0


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def test_end_to_end_a_move_to_a_new_module_with_a_multi_line_caller_passes(repo: Path):
    """The re-review's repro: c.py imports foo over several lines and is unchanged; foo moves from a.py to b.py."""
    write(repo, "src/pkg/a.py", "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n")
    write(repo, "src/pkg/c.py", "from pkg.a import (\n    bar,\n    foo,\n)\n\n\ndef run():\n    return foo() + bar()\n")
    write(repo, CLI, "from pkg.c import run\n\n\ndef main(argv):\n    return run()\n")
    commit_all(repo, "a and c")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "src/pkg/b.py", "def foo():\n    return 1\n")
    write(repo, "src/pkg/a.py", "from pkg.b import foo  # noqa: F401\n\n\ndef bar():\n    return 2\n")
    commit_all(repo, "move foo to b")
    result = check(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_end_to_end_a_move_out_of_a_package_init_into_a_reexported_submodule_passes(repo: Path):
    write(repo, "src/pkg/__init__.py", "def helper():\n    return 1\n")
    write(repo, CLI, "from pkg import helper\n\n\ndef main(argv):\n    return helper()\n")
    commit_all(repo, "helper in the package")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "src/pkg/parts.py", "def helper():\n    return 1\n")
    write(repo, "src/pkg/__init__.py", "from .parts import helper  # noqa: F401\n")
    commit_all(repo, "move helper to parts")
    result = check(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_end_to_end_an_h9_split_with_callers_unchanged_passes_and_a_new_helper_in_it_still_fails(repo: Path):
    """Three functions leave a big module for a new submodule, the old module re-exports them, no caller changes."""
    big = "".join(f"def step_{n}(x):\n    return x + {n}\n\n\n" for n in range(1, 6))
    write(repo, "src/pkg/big.py", big)
    write(repo, CLI, "from pkg.big import (\n" + "".join(f"    step_{n},\n" for n in range(1, 6)) + ")\n\n\n"
                     "def main(argv):\n    return " + " + ".join(f"step_{n}(0)" for n in range(1, 6)) + "\n")
    commit_all(repo, "big module")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, "src/pkg/big_steps.py", "".join(f"def step_{n}(x):\n    return x + {n}\n\n\n" for n in (1, 2, 3)))
    write(repo, "src/pkg/big.py", "from pkg.big_steps import step_1, step_2, step_3  # noqa: F401\n\n\n"
                                  + "".join(f"def step_{n}(x):\n    return x + {n}\n\n\n" for n in (4, 5)))
    commit_all(repo, "split big")
    moved = check(repo)
    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert "1 added public symbol(s)" in moved.stdout  # the new module, imported by big.py

    with (repo / "src/pkg/big_steps.py").open("a", encoding="utf-8") as steps:
        steps.write("def step_6(x):\n    return x + 6\n")
    added = check(repo)
    assert added.returncode == 1
    assert "function step_6" in added.stdout and "step_1" not in added.stdout
