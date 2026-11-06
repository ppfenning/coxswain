"""The import dependency map of components and test modules."""

from __future__ import annotations

from pathlib import Path

from harness.check_deps import DepMap, build_dep_map, dependents, load_dep_map

DIRS = ["src/tools", "src/graphs", "src/cartridges"]
TOOLS, GRAPHS, CARTS = DIRS

FILES = {
    "src/tools/agent_tools/dash.py": "from harness import lanes\n",
    "src/tools/tests/test_dash.py": "from harness.lanes import run\nimport agent_tools.dash\n",
    "src/graphs/harness/__init__.py": "",
    "src/graphs/harness/lanes.py": "import core.policy\n",
    "src/graphs/harness/lonely.py": "import os\n",
    "src/cartridges/core/__init__.py": "",
    "src/cartridges/core/policy.py": "X = 1\n",
}


def test_tools_file_importing_harness_lists_graphs() -> None:
    dep = build_dep_map(FILES, DIRS)
    assert dep.components[TOOLS] == {GRAPHS}
    assert dep.components[GRAPHS] == {CARTS}
    assert dep.components[CARTS] == frozenset()


def test_graphs_file_imported_by_a_tools_test_is_in_its_reach() -> None:
    reach = build_dep_map(FILES, DIRS).test_reach["src/tools/tests/test_dash.py"]
    assert "src/graphs/harness/lanes.py" in reach
    assert "src/graphs/harness/__init__.py" in reach


def test_reach_is_transitive_across_components() -> None:
    files = {
        "src/tools/tests/test_a.py": "import agent_tools.a\n",
        "src/tools/agent_tools/a.py": "import harness.b\n",
        "src/graphs/harness/b.py": "import core.c\n",
        "src/cartridges/core/c.py": "",
    }
    reach = build_dep_map(files, DIRS).test_reach["src/tools/tests/test_a.py"]
    assert reach == {"src/tools/agent_tools/a.py", "src/graphs/harness/b.py", "src/cartridges/core/c.py"}


def test_reach_survives_an_import_cycle() -> None:
    files = {
        "src/graphs/tests/test_a.py": "import harness.a\n",
        "src/graphs/harness/a.py": "import harness.b\n",
        "src/graphs/harness/b.py": "import harness.a\n",
    }
    reach = build_dep_map(files, DIRS).test_reach["src/graphs/tests/test_a.py"]
    assert reach == {"src/graphs/harness/a.py", "src/graphs/harness/b.py"}


def test_relative_import_is_followed_within_a_component() -> None:
    files = {
        "src/graphs/tests/test_a.py": "import harness.a\n",
        "src/graphs/harness/__init__.py": "",
        "src/graphs/harness/a.py": "from . import b\n",
        "src/graphs/harness/b.py": "",
    }
    assert "src/graphs/harness/b.py" in build_dep_map(files, DIRS).test_reach["src/graphs/tests/test_a.py"]


def test_syntax_error_is_recorded_not_raised() -> None:
    dep = build_dep_map({"src/tools/agent_tools/bad.py": "def (:\n", **FILES}, DIRS)
    assert dep.unresolved == {"src/tools/agent_tools/bad.py"}
    assert dep.components[TOOLS] == {GRAPHS}


def test_dynamic_import_with_a_computed_name_is_unresolved() -> None:
    files = {
        "src/tools/agent_tools/dyn.py": "import importlib\nimportlib.import_module(name)\n",
        "src/tools/agent_tools/lit.py": "import importlib\nimportlib.import_module('core.policy')\n",
        **FILES,
    }
    dep = build_dep_map(files, DIRS)
    assert dep.unresolved == {"src/tools/agent_tools/dyn.py"}
    assert dep.components[TOOLS] == {GRAPHS, CARTS}


def test_dependents_of_a_shared_graphs_module_reach_tools() -> None:
    dep = build_dep_map(FILES, DIRS)
    assert dependents(dep, {"src/graphs/harness/lanes.py"}, DIRS) == {GRAPHS, TOOLS}


def test_dependents_of_a_module_nobody_imports_is_its_own_component() -> None:
    dep = build_dep_map(FILES, DIRS)
    assert dependents(dep, {"src/tools/agent_tools/dash.py"}, DIRS) == {TOOLS}


def test_dependents_of_a_path_outside_every_component_is_empty() -> None:
    assert dependents(build_dep_map(FILES, DIRS), {"README.md"}, DIRS) == frozenset()


def test_the_record_is_frozen() -> None:
    dep = build_dep_map({}, DIRS)
    assert isinstance(dep, DepMap) and dep.unresolved == frozenset()
    try:
        dep.unresolved = frozenset({"x"})  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("DepMap accepted an assignment")


def test_reader_skips_vendored_directories(tmp_path: Path) -> None:
    for rel, text in {
        "src/tools/agent_tools/a.py": "import harness.b\n",
        "src/graphs/harness/b.py": "",
        "src/graphs/node_modules/x/c.py": "import core\n",
        "src/graphs/.venv/lib/d.py": "import core\n",
        "src/graphs/harness/notes.txt": "not python",
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    dep = load_dep_map(tmp_path, DIRS)
    assert dep.components[TOOLS] == {GRAPHS}
    assert dep.components[GRAPHS] == frozenset()
