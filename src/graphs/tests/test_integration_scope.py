"""Selection of integration checks by touched package."""

from __future__ import annotations

import copy

from harness.integration_scope import package_of, scope_integration

FILL = {"name": "pkgs", "cmd": "pytest {packages}", "timeout": 90}
PLAIN = {"name": "plain", "cmd": "make smoke"}


def test_package_of_deep_path() -> None:
    assert package_of("src/graphs/harness/epic.py") == "src/graphs"


def test_package_of_shallow_path() -> None:
    assert package_of("src/x.py") == "src"


def test_package_of_root_level_path() -> None:
    assert package_of("README.md") == "README.md"


def test_package_of_deep_path_outside_source_tree() -> None:
    assert package_of("docs/customize/index.md") == "docs"


def test_placeholder_filled_with_sorted_packages() -> None:
    got = scope_integration([FILL], ["src/tools/a/b.py", "src/graphs/harness/epic.py"])
    assert got.checks == ({"name": "pkgs", "cmd": "pytest src/graphs src/tools", "timeout": 90},)
    assert got.selection == {"packages": ["src/graphs", "src/tools"], "reason": "packages touched", "ran": ["pkgs"]}


def test_check_without_placeholder_runs_unchanged() -> None:
    assert scope_integration([PLAIN], ["src/graphs/harness/epic.py"]).checks == (PLAIN,)


def test_extra_keys_survive_the_fill() -> None:
    assert scope_integration([FILL], ["src/graphs/a/b.py"]).checks[0]["timeout"] == 90


def test_no_changed_paths_selects_nothing() -> None:
    got = scope_integration([FILL, PLAIN], [])
    assert got.checks == ()
    assert got.selection == {"packages": [], "reason": "nothing touched", "ran": []}


def test_duplicate_packages_collapsed() -> None:
    got = scope_integration([FILL], ["src/graphs/a/x.py", "src/graphs/b/y.py"])
    assert got.selection["packages"] == ["src/graphs"]


def test_test_file_counts_as_touching_its_package() -> None:
    assert scope_integration([FILL], ["src/graphs/tests/test_x.py"]).selection["packages"] == ["src/graphs"]


def test_inputs_not_mutated() -> None:
    checks, paths = [FILL, PLAIN], ["src/b/c/d.py", "src/a/c/d.py"]
    before = copy.deepcopy((checks, paths))
    scope_integration(checks, paths)
    assert (checks, paths) == before
