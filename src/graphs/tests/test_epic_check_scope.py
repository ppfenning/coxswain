"""The lane build runs only the pytest checks a task's patch can affect, and records what it chose."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from test_epic_driver import (  # noqa: F401 -- fixtures
    Repairing,
    _broken_probe,
    cart,
    drive,
    git,
    initiative,
    new_file_patch,
    repo,
)

import harness.epic as epic

LINT, TOOLS, GRAPHS, CLEAN = "echo lint", "cd tools && pytest -q", "cd graphs && pytest -q", "echo clean"
CHECKS = [
    {"name": "lint", "cmd": LINT},
    {"name": "tools", "cmd": TOOLS},
    {"name": "graphs", "cmd": GRAPHS},
    {"name": "clean", "cmd": CLEAN},
]
TOOLS_TEST = "cd tools && pytest -q tests/test_app.py"
LANE = "checks/p1-foundations/t1-probe/"
TOOLS_EDIT = "tools/app.py"
RENAME = (
    "diff --git a/tools/app.py b/tools/app2.py\nsimilarity index 100%\n"
    "rename from tools/app.py\nrename to tools/app2.py\n"
)


def edit_patch(name: str, context: str, old: str, new: str) -> str:
    return f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1,2 +1,2 @@\n {context}\n-{old}\n+{new}\n"


def tools_edit() -> str:
    return edit_patch(TOOLS_EDIT, "import gcore", "NAME = 'app'", "NAME = 'tools'")


@pytest.fixture
def tree(repo: Path) -> Path:  # noqa: F811
    """Two components: tools imports graphs, graphs imports nothing."""
    files = {
        "graphs/gcore.py": "# core\nVALUE = 1\n",
        "graphs/tests/test_gcore.py": "import gcore\n\n\ndef test_value():\n    assert gcore.VALUE\n",
        "tools/app.py": "import gcore\nNAME = 'app'\n",
        "tools/tests/test_app.py": "import app\n\n\ndef test_name():\n    assert app.NAME\n",
        "tools/other.py": "OTHER = 1\n",
        "tools/tests/test_other.py": "import other\n\n\ndef test_other():\n    assert other.OTHER\n",
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "components", cwd=repo)
    return repo


@pytest.fixture
def lane(monkeypatch) -> dict:
    """Every lane `run_checks` call as its commands, with the context and record `_build_task` returned."""
    seen: dict = {"lane_calls": [], "ctx": None, "record": None}
    build = epic._build_task

    def counts(cmd: str) -> dict[str, int]:
        return {"passed": 3, "failed": 0, "skipped": 5} if "pytest" in cmd else {}

    def fake_run_checks(worktree, checks, **kw):
        if kw.get("log_prefix") == LANE:
            seen["lane_calls"].append([c["cmd"] for c in checks])
        return [
            {
                "name": c["name"],
                "cmd": c["cmd"],
                "passed": True,
                "exit_code": 0,
                "counts": counts(c["cmd"]),
                "cpu_seconds": 1.5,
            }
            for c in checks
        ]

    def spy_build(ctx, **kw):
        seen["ctx"], seen["record"] = ctx, build(ctx, **kw)
        return seen["record"]

    monkeypatch.setattr(epic, "run_checks", fake_run_checks)
    monkeypatch.setattr(epic, "_build_task", spy_build)
    return seen


@pytest.fixture
def run(tree, cart, tmp_path, lane) -> Callable[[str], dict]:  # noqa: F811
    """Drive one task whose patch is the argument; returns `lane`."""

    def run_patch(patch: str) -> dict:
        cart["landing_areas"]["checks"] = CHECKS
        work = initiative(two_phases=False)
        work["items"] = work["items"][:1]
        drive(tree, cart, tmp_path, work=work, patches={"t1-probe": patch})
        return lane

    return run_patch


def test_a_tools_only_patch_runs_tools_and_the_pass_through_commands(run) -> None:
    assert run(tools_edit())["lane_calls"] == [[LINT, TOOLS_TEST, CLEAN]]


def test_a_graphs_patch_that_tools_imports_runs_both(run) -> None:
    (calls,) = run(edit_patch("graphs/gcore.py", "# core", "VALUE = 1", "VALUE = 2"))["lane_calls"]
    assert [c for c in calls if c in (LINT, TOOLS, CLEAN)] == [LINT, TOOLS, CLEAN]
    assert any(c.startswith(GRAPHS) for c in calls)


def test_a_patch_adding_a_test_file_always_runs_it(run) -> None:
    seen = run(new_file_patch("tools/tests/test_extra.py", "def test_x(): pass"))
    assert seen["lane_calls"] == [[LINT, f"{TOOLS} tests/test_extra.py", CLEAN]]
    assert seen["record"]["check_scope"]["added_tests"] == ["tools/tests/test_extra.py"]


def test_the_evidence_names_the_selection_and_the_tests_run(run) -> None:
    record = run(tools_edit())["record"]
    (row,) = [r for r in record["evidence"] if r["check"] == "checks:scope"]
    assert row["output"] == (
        "scoped checks: ran tools (narrowed to 1 of 2 files); skipped graphs; tests_run=3; tests 1 of 2, cpu 4.5s"
    )
    assert record["check_scope"]["tests_run"] == 3
    assert record["check_scope"]["components_skipped"] == ["graphs"]


def test_the_context_keeps_the_full_check_list(run) -> None:
    assert [c["cmd"] for c in run(tools_edit())["ctx"].checks] == [c["cmd"] for c in CHECKS]


def test_a_failure_building_the_map_runs_the_full_list_and_says_why(run, monkeypatch) -> None:
    def broken(*args):
        raise RuntimeError("cannot read sources")

    monkeypatch.setattr(epic, "load_dep_map", broken)
    seen = run(tools_edit())
    assert seen["lane_calls"] == [[c["cmd"] for c in CHECKS]]
    reasons = seen["record"]["check_scope"]["reason"]
    assert set(reasons) == {"tools", "graphs"}
    assert all("import map failed: RuntimeError: cannot read sources" in why for why in reasons.values())
    assert any(r["check"] == "checks:scope" and "tests_run=6" in r["output"] for r in seen["record"]["evidence"])
    scope = seen["record"]["check_scope"]
    assert "tests" not in scope and "totals" not in scope
    assert not any(
        "tests " in r["output"].split("tests_run")[-1]
        for r in seen["record"]["evidence"]
        if r["check"] == "checks:scope"
    )
    assert seen["record"]["check_cpu_seconds"] == 6.0


def test_a_one_module_patch_records_totals_with_selected_below_total(run) -> None:
    record = run(tools_edit())["record"]
    assert record["check_scope"]["tests"] == {"tools": {"selected": 1, "total": 2}}
    assert record["check_scope"]["totals"] == {"tests_selected": 1, "tests_total": 2, "cpu_seconds": 4.5}
    assert record["check_cpu_seconds"] == 4.5


def test_a_conftest_patch_records_selected_equal_to_total(run) -> None:
    record = run(new_file_patch("tools/tests/conftest.py", "X = 1"))["record"]
    totals = record["check_scope"]["totals"]
    assert totals["tests_selected"] == totals["tests_total"] == 2


def test_scope_figures_sum_across_components_and_drop_out_without_counts() -> None:
    tests = {"a": {"selected": 1, "total": 4}, "b": {"selected": 2, "total": 3}}
    results = [{"cpu_seconds": 1.2344}, {"cpu_seconds": 2.0}, {}]
    assert epic._scope_figures({"tests": tests}, results)["totals"] == {
        "tests_selected": 3,
        "tests_total": 7,
        "cpu_seconds": 3.234,
    }
    assert epic._scope_figures({}, results) == {}
    assert epic._scope_clause(epic._scope_figures({"tests": tests}, results)) == "; tests 3 of 7, cpu 3.2s"
    assert epic._scope_clause({}) == ""


def test_an_added_source_module_runs_its_component_whole(run) -> None:
    seen = run(new_file_patch("tools/newmod.py", "X = 1"))
    assert seen["lane_calls"] == [[LINT, TOOLS, CLEAN]]
    assert seen["record"]["check_scope"]["added_tests"] == []


def test_an_unreadable_diff_runs_the_full_list_and_says_why(run, monkeypatch) -> None:
    real = epic._git

    def git_without_diff(*args, **kw):
        return (False, "bad ref") if "--name-only" in args else real(*args, **kw)

    monkeypatch.setattr(epic, "_git", git_without_diff)
    seen = run(tools_edit())
    assert seen["lane_calls"] == [[c["cmd"] for c in CHECKS]]
    assert set(seen["record"]["check_scope"]["reason"].values()) == {"fallback diff unreadable: bad ref"}


def test_a_rename_hands_the_selector_both_paths(run, monkeypatch) -> None:
    real, listed = epic.select_checks, []

    def spy(checks, changed, *rest):
        listed.append(list(changed))
        return real(checks, changed, *rest)

    monkeypatch.setattr(epic, "select_checks", spy)
    run(RENAME)
    assert {"tools/app.py", "tools/app2.py"} <= set(listed[0])


def test_a_style_pass_runs_a_narrowed_command_and_records_the_totals(tree, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(epic, "_collected_ids", lambda _worktree: {"test_x.py::test_a"})
    real, runs = epic.run_checks, []

    def run_checks(worktree, checks, **kw):
        """The lint check runs for real; the pytest check is faked so no tools tree has to import."""
        if kw.get("log_prefix") == LANE:
            runs.append([c["cmd"] for c in checks])
        live = [c for c in checks if "pytest" not in c["cmd"]]
        fake = [
            {"name": c["name"], "cmd": c["cmd"], "passed": True, "exit_code": 0, "cpu_seconds": 2.0}
            for c in checks
            if "pytest" in c["cmd"]
        ]
        return [*real(worktree, live, **kw), *fake]

    builds, build = {}, epic._build_task

    def spy_build(ctx, **kw):
        builds[kw["task"]] = record = build(ctx, **kw)
        return record

    monkeypatch.setattr(epic, "run_checks", run_checks)
    monkeypatch.setattr(epic, "_build_task", spy_build)
    lint = f"{sys.executable} check.py"
    cart["skills"]["style_pass"] = "acme-skills:style"
    cart["landing_areas"]["checks"] = [{"name": "lint", "cmd": lint}, {"name": "tools", "cmd": TOOLS}]
    patches = {**_broken_probe(), "t1-probe": new_file_patch("t1-probe.txt", "broken") + tools_edit()}
    result, _ = drive(tree, cart, tmp_path, runner=Repairing(patches, repaired={}), work=initiative(two_phases=False))
    task = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert task["refix"] == ["style_pass"]
    assert runs[-1] == [lint, TOOLS_TEST]
    record = builds["t1-probe"]
    totals = record["check_scope"]["totals"]
    assert (totals["tests_selected"], totals["tests_total"]) == (1, 2)
    assert totals["cpu_seconds"] == record["check_cpu_seconds"] >= 2.0
    (row,) = [r["output"] for r in task["evidence"] if r["check"] == "checks:scope"]
    assert row.startswith(
        "style pass: scoped checks: ran tools (narrowed to 1 of 2 files); tests_run=1; tests 1 of 2, cpu "
    )
