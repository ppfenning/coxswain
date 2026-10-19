"""The harness interpreter rule, at the pure deciders with literal inputs."""

from pathlib import Path

from agent_tools import install_root

CHECKOUT = Path("/h/installs/coxswain")
HARNESS = CHECKOUT / "src" / "graphs"


def test_project_name_reads_the_project_table():
    assert install_root.project_name('[project]\nname = "coxswain"\n') == "coxswain"


def test_project_name_is_none_for_text_that_does_not_parse():
    assert install_root.project_name("[project\n") is None


def test_nearest_checkout_is_the_closest_ancestor_named_coxswain():
    names = {HARNESS: None, CHECKOUT / "src": None, CHECKOUT: "coxswain", Path("/h"): "coxswain"}
    assert install_root.nearest_checkout(HARNESS, names) == CHECKOUT


def test_nearest_checkout_skips_a_pyproject_with_another_name():
    old = Path("/h/installs/coxswain-graphs")
    assert install_root.nearest_checkout(old, {old: "coxswain-graphs"}) is None


def test_a_harness_venv_that_exists_wins_on_an_unmigrated_host():
    chosen = install_root.choose_python(HARNESS, legacy_exists=True, checkout=CHECKOUT)
    assert chosen == HARNESS / ".venv" / "bin" / "python"


def test_without_a_harness_venv_the_checkout_venv_runs_the_harness():
    chosen = install_root.choose_python(HARNESS, legacy_exists=False, checkout=CHECKOUT)
    assert chosen == CHECKOUT / ".venv" / "bin" / "python"


def test_with_neither_the_missing_harness_venv_is_named():
    chosen = install_root.choose_python(HARNESS, legacy_exists=False, checkout=None)
    assert chosen == HARNESS / ".venv" / "bin" / "python"


def test_paths_inside_one_checkout_collapse_to_its_root_once():
    paths = [f"{HARNESS}", f"{CHECKOUT}/src/cartridges/providers", f"{CHECKOUT}/src/tools"]
    roots = dict.fromkeys(paths, CHECKOUT)
    assert install_root.collapse_to_checkouts(paths, roots) == [str(CHECKOUT)]


def test_paths_outside_a_checkout_are_kept_as_they_are():
    paths = ["/h/installs/coxswain-graphs", "/h/installs/coxswain-tools"]
    assert install_root.collapse_to_checkouts(paths, {}) == paths
