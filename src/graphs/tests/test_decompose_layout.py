from __future__ import annotations

import pytest

from graphs.delivery import initiative_decompose
from graphs.delivery.decompose_layout import check_layout
from runner import ScriptedRunner


def task(id_: str, body: str, surfaces: list[str], needs: list[str] | None = None) -> dict:
    return {"id": id_, "phase": "p1", "title": id_, "body": body, "surfaces": surfaces, "needs": needs or []}


CLEAN = [
    task(
        "a", "Add src/widget.py and cover it in tests/test_widget.py.", ["src/widget.py", "tests/test_widget.py (new)"]
    ),
    task("b", "Read src/widget.py, then wire it in src/app.py.", ["src/app.py"], ["a"]),
]


def rules(report) -> list[tuple[str, str]]:
    return [(f.task, f.rule) for f in report.findings]


def test_a_clean_decomposition_has_no_findings_and_a_chain_has_width_one():
    report = check_layout(CLEAN)
    assert report.findings == ()
    assert report.width == 1


def test_a_body_missing_a_surface_is_a_blocking_finding():
    report = check_layout([task("a", "Edit src/widget.py only.", ["src/widget.py", "src/other.py"])])
    assert rules(report) == [("a", "surface_not_named")]
    assert "src/other.py" in report.findings[0].detail
    assert report.blocking == report.findings


def test_a_body_that_names_no_path_is_flagged_without_blocking():
    report = check_layout([task("a", "Do the work.", ["src/widget.py"])])
    assert rules(report) == [("a", "surface_not_named")]
    assert report.blocking == ()


def test_a_test_filename_that_differs_from_its_surface_is_a_finding():
    body = "Edit src/widget.py. Put the cases in tests/test_widget_new.py and name tests/test_widget.py too."
    report = check_layout([task("a", body, ["src/widget.py", "tests/test_widget.py"])])
    assert rules(report) == [("a", "artifact_not_surface")]
    assert "tests/test_widget_new.py" in report.findings[0].detail


def test_a_migration_and_a_script_in_the_body_must_be_surfaces():
    body = "Edit src/db.py, add migrations/0007_add_col.sql and run scripts/backfill.py."
    report = check_layout([task("a", body, ["src/db.py", "migrations/0007_add_col.sql"])])
    assert [f.detail for f in report.findings] == ["body names scripts/backfill.py, which is not among the surfaces"]


def test_a_needs_edge_with_no_data_flow_in_the_body_is_advisory():
    tasks = [task("a", "Add src/a.py.", ["src/a.py"]), task("b", "Add src/b.py.", ["src/b.py"], ["a"])]
    report = check_layout(tasks)
    assert [(f.rule, f.advisory) for f in report.findings] == [("edge_unsupported", True)]
    assert report.blocking == ()


def test_a_wide_graph_reports_its_width():
    root = task("root", "Add src/root.py.", ["src/root.py"])
    leaves = [task(f"l{i}", f"Use src/root.py in src/l{i}.py.", [f"src/l{i}.py"], ["root"]) for i in range(4)]
    report = check_layout([root, *leaves])
    assert report.findings == ()
    assert report.width == 4
    assert report.layers == (("root",), ("l0", "l1", "l2", "l3"))


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["decompose"] = "acme-skills:decompose"
    cartridge["work_routing"] = {"states": {"active": "work", "planned": "work", "future": "backlog"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "deferred"}
    cartridge["write_kinds"]["state_move"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


def decomposition(tasks: list[dict]) -> dict:
    return {"phases": [{"id": "p1", "goal": "g"}], "tasks": tasks}


def test_the_run_outcome_carries_the_layout_and_width(cart):
    runner = ScriptedRunner({"decompose": [decomposition(CLEAN)]})
    result = initiative_decompose.run({"run_id": "r", "date": "d", "idea": "x", "cartridge": cart}, runner)
    assert result["layout"]["width"] == 1
    assert result["layout"]["findings"] == []


def test_a_blocking_layout_finding_refuses_the_run(cart):
    bad = task("a", "Edit src/widget.py only.", ["src/widget.py", "src/other.py"])
    runner = ScriptedRunner({"decompose": [decomposition([bad])]})
    with pytest.raises(initiative_decompose.LintRefusal, match="lint-refused: a surface_not_named"):
        initiative_decompose.run({"run_id": "r", "date": "d", "idea": "x", "cartridge": cart}, runner)
