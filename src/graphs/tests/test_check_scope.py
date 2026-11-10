"""The check scope selector, from literal import maps, plus one test over this repository's real src/tools map."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from harness.check_deps import load_dep_map
from harness.check_scope import ScopedChecks, pytest_component, render_selection, select_checks

TOOLS = {"name": "tools", "cmd": "cd src/tools && pytest -q -n auto"}
GRAPHS = {"name": "graphs", "cmd": "cd src/graphs && pytest -q -n auto"}
CARTRIDGES = {"name": "cartridges", "cmd": "cd src/cartridges && pytest -q -n auto"}
CLEAN = {"name": "clean", "cmd": "python .github/check_clean.py . --require-names"}
LINT = {"name": "ruff", "cmd": "ruff check ."}
CHECKS = [LINT, TOOLS, GRAPHS, CARTRIDGES, CLEAN]

IMPORT_MAP = {
    "components": {
        "src/tools": frozenset({"src/graphs", "src/cartridges"}),
        "src/graphs": frozenset({"src/cartridges"}),
        "src/cartridges": frozenset(),
    },
    "test_reach": {
        "src/tools/tests/test_a.py": frozenset({"src/tools/a.py"}),
        "src/tools/tests/test_b.py": frozenset({"src/tools/b.py", "src/tools/a.py"}),
        "src/tools/tests/test_c.py": frozenset({"src/tools/c.py"}),
        "src/graphs/tests/test_g.py": frozenset({"src/graphs/g.py"}),
        "src/cartridges/tests/test_k.py": frozenset({"src/cartridges/k.py"}),
    },
    "unresolved": frozenset({"src/tools/dynamic.py"}),
}


def scope(paths: list[str], added: set[str] | None = None) -> ScopedChecks:
    return select_checks(CHECKS, paths, added or set(), IMPORT_MAP)


def names(scoped: ScopedChecks) -> list[str]:
    return [c["name"] for c in scoped.checks]


def test_pytest_component_reads_the_cd_directory() -> None:
    assert pytest_component("cd src/tools && pytest -q") == "src/tools"
    assert pytest_component("ruff check .") is None
    assert pytest_component("cd src/tools && ruff check .") is None
    assert pytest_component("pytest -q") is None


def test_tools_only_patch_keeps_only_the_tools_command() -> None:
    scoped = scope(["src/tools/c.py"])
    assert names(scoped) == ["ruff", "tools", "clean"]
    assert scoped.selection["components_run"] == ["src/tools"]
    assert scoped.selection["components_skipped"] == ["src/graphs", "src/cartridges"]


def test_graphs_patch_keeps_graphs_and_the_tools_that_import_it() -> None:
    scoped = scope(["src/graphs/g.py"])
    assert names(scoped) == ["ruff", "tools", "graphs", "clean"]
    assert scoped.selection["reason"] == {"src/tools": "imports src/graphs", "src/graphs": "changed"}
    assert scoped.selection["narrowed"]["src/tools"] == "whole"
    assert scoped.selection["narrowed"]["src/graphs"] == ["tests/test_g.py"]


def test_dependent_with_its_own_change_still_runs_whole_when_its_dependency_changed() -> None:
    scoped = scope(["src/graphs/g.py", "src/tools/c.py"])
    assert names(scoped) == ["ruff", "tools", "graphs", "clean"]
    assert scoped.checks[1]["cmd"] == TOOLS["cmd"]
    assert scoped.selection["narrowed"] == {"src/tools": "whole", "src/graphs": ["tests/test_g.py"]}
    assert scoped.selection["reason"]["src/tools"] == "imports src/graphs"


def test_dependent_with_an_added_test_runs_whole_when_its_dependency_changed() -> None:
    new = "src/tools/tests/test_new.py"
    scoped = scope(["src/graphs/g.py", new], {new})
    assert scoped.checks[1]["cmd"] == TOOLS["cmd"]
    assert scoped.selection["narrowed"]["src/tools"] == "whole"


def test_added_test_in_a_dependency_does_not_pull_in_its_dependents() -> None:
    new = "src/graphs/tests/test_new.py"
    scoped = scope([new], {new})
    assert names(scoped) == ["ruff", "graphs", "clean"]
    assert scoped.selection["reason"] == {"src/graphs": "new test"}


def test_cartridges_only_patch_keeps_all_three_components() -> None:
    scoped = scope(["src/cartridges/k.py"])
    assert names(scoped) == ["ruff", "tools", "graphs", "cartridges", "clean"]
    assert scoped.selection["components_skipped"] == []
    assert scoped.selection["reason"]["src/graphs"] == "imports src/cartridges"


def test_new_test_file_under_an_unaffected_component_still_runs_narrowed() -> None:
    new = "src/cartridges/tests/test_new.py"
    scoped = scope([new], {new})
    assert names(scoped) == ["ruff", "cartridges", "clean"]
    assert scoped.checks[1]["cmd"] == "cd src/cartridges && pytest -q -n auto tests/test_new.py"
    assert scoped.selection["reason"] == {"src/cartridges": "new test"}
    assert scoped.selection["added_tests"] == [new]


def test_changed_non_python_file_falls_back_to_the_whole_component() -> None:
    scoped = scope(["src/tools/data.json"])
    assert scoped.checks[1]["cmd"] == TOOLS["cmd"]
    assert scoped.selection["narrowed"]["src/tools"] == "whole"
    assert scoped.selection["reason"]["src/tools"] == "fallback non-Python file src/tools/data.json"


def test_unresolved_unmapped_and_conftest_changes_fall_back_to_whole() -> None:
    reasons = {
        "src/tools/dynamic.py": "fallback unresolved src/tools/dynamic.py",
        "src/tools/new_mod.py": "fallback unmapped src/tools/new_mod.py",
        "src/tools/conftest.py": "fallback conftest.py changed",
        "src/tools/pytest.ini": "fallback pytest.ini changed",
    }
    for path, reason in reasons.items():
        selection = scope([path]).selection
        assert selection["narrowed"]["src/tools"] == "whole"
        assert selection["reason"]["src/tools"] == reason


def test_check_clean_and_lint_pass_through_unchanged() -> None:
    scoped = scope(["src/tools/c.py"])
    assert scoped.checks[0] == LINT
    assert scoped.checks[-1] == CLEAN


def test_empty_patch_returns_the_full_list() -> None:
    scoped = scope([])
    assert list(scoped.checks) == CHECKS
    assert scoped.selection["components_skipped"] == []


def test_paths_under_no_known_component_return_the_full_list() -> None:
    assert list(scope(["docs/index.md"]).checks) == CHECKS


def test_narrowing_keeps_changed_sources_tests_and_added_tests() -> None:
    new = "src/tools/tests/test_new.py"
    scoped = scope(["src/tools/a.py", new], {new})
    assert (
        scoped.checks[1]["cmd"] == "cd src/tools && pytest -q -n auto tests/test_a.py tests/test_b.py tests/test_new.py"
    )
    assert scoped.selection["narrowed"]["src/tools"] == ["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"]


def test_render_selection_is_one_short_line() -> None:
    selection = scope(["src/tools/c.py"]).selection
    assert render_selection(selection) == (
        "scoped checks: ran src/tools (narrowed to 1 of 3 files); skipped src/graphs, src/cartridges"
    )
    whole = scope(["src/tools/data.json"]).selection
    assert render_selection(whole).startswith("scoped checks: ran src/tools (whole);")


def test_render_selection_of_a_whole_selection_without_a_tests_key() -> None:
    selection = {"components_run": ["tools"], "components_skipped": [], "narrowed": {"tools": "whole"}}
    assert render_selection(selection) == "scoped checks: ran tools (whole)"


def test_a_patch_selects_direct_and_transitive_importers_with_counts() -> None:
    selection = scope(["src/tools/a.py"]).selection
    assert selection["narrowed"]["src/tools"] == ["tests/test_a.py", "tests/test_b.py"]
    assert selection["tests"] == {"src/tools": {"selected": 2, "total": 3}}
    assert render_selection(selection).startswith("scoped checks: ran src/tools (narrowed to 2 of 3 files);")


def test_conftest_change_selects_the_whole_component_with_selected_equal_to_total() -> None:
    selection = scope(["src/tools/tests/conftest.py"]).selection
    assert selection["narrowed"]["src/tools"] == "whole"
    assert selection["reason"]["src/tools"] == "fallback conftest.py changed"
    assert selection["tests"]["src/tools"] == {"selected": 3, "total": 3}


def test_fixture_directory_change_selects_the_whole_component() -> None:
    path = "src/tools/tests/fixtures/data.json"
    selection = scope([path]).selection
    assert selection["narrowed"]["src/tools"] == "whole"
    assert selection["reason"]["src/tools"] == f"fallback non-Python file {path}"
    assert selection["tests"]["src/tools"] == {"selected": 3, "total": 3}


def test_full_fallback_counts_every_component_whole() -> None:
    assert scope([]).selection["tests"] == {
        "src/tools": {"selected": 3, "total": 3},
        "src/graphs": {"selected": 1, "total": 1},
        "src/cartridges": {"selected": 1, "total": 1},
    }


def test_real_tools_map_narrows_a_leaf_module_to_under_15_percent_of_tests() -> None:
    root = Path(__file__).resolve().parents[3]
    dep_map = load_dep_map(root, ["src/tools"])
    reach = dep_map.test_reach
    imports = Counter(src for srcs in reach.values() for src in srcs)
    leaf = min(imports, key=lambda src: (imports[src], src))
    import_map = {"components": dep_map.components, "test_reach": reach, "unresolved": dep_map.unresolved}
    checks = [{"name": "tools", "cmd": "cd src/tools && pytest -q"}]
    selection = select_checks(checks, [leaf], set(), import_map).selection
    counts = selection["tests"]["src/tools"]
    assert counts["total"] == len(reach) > 0
    assert 0 < counts["selected"] < 0.15 * counts["total"], (leaf, counts)
