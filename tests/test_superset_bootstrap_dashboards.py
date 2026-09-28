import importlib.util
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load("superset_bootstrap_dashboards", DEPLOY / "bootstrap.py")

DATABASE = {"name": "db", "sqlalchemy_uri": "duckdb:///:memory:"}
DATASETS = [{"name": "runs", "database": "db", "sql": "select 1", "requires": "sqlite"}]
CHARTS = [{"name": "Chart A", "dataset": "runs", "viz_type": "big_number", "params": {"metrics": [{"label": "count"}]}}]
PRESENT = frozenset({"sqlite"})


def _specs(dashboards):
    return {"database": DATABASE, "datasets": DATASETS, "charts": CHARTS, "dashboard": dashboards[0], "dashboards": dashboards}


def test_three_dashboards_with_live_charts_give_three_dashboard_ops_in_list_order_after_every_chart_op():
    dashboards = [
        {"name": "Coxswain", "grid": [[{"chart": "Chart A", "width": 12}]]},
        {"name": "Fleet", "grid": [[{"chart": "Chart A", "width": 12}]]},
        {"name": "Chair", "grid": [[{"chart": "Chart A", "width": 12}]]},
    ]
    ops = bootstrap.plan(_specs(dashboards), {}, PRESENT)
    board_ops = [o for o in ops if o.kind == "dashboard"]
    assert [o.name for o in board_ops] == ["Coxswain", "Fleet", "Chair"]
    last_chart_index = max(i for i, o in enumerate(ops) if o.kind == "chart")
    first_board_index = min(i for i, o in enumerate(ops) if o.kind == "dashboard")
    assert first_board_index > last_chart_index


def test_a_listed_dashboard_whose_every_chart_is_skipped_gives_a_skipped_op_and_the_others_are_still_planned():
    dashboards = [
        {"name": "Coxswain", "grid": [[{"chart": "Chart A", "width": 12}]]},
        {"name": "Fleet", "grid": [[{"chart": "Missing Chart", "width": 12}]]},
    ]
    ops = bootstrap.plan(_specs(dashboards), {}, PRESENT)
    board_ops = {o.name: o for o in ops if o.kind == "dashboard"}
    assert board_ops["Fleet"].action == "skipped"
    assert board_ops["Fleet"].reason == "no charts"
    assert board_ops["Coxswain"].action == "create"


def test_specs_with_no_dashboards_key_plan_exactly_the_one_board_from_dashboard():
    specs = {
        "database": DATABASE,
        "datasets": DATASETS,
        "charts": CHARTS,
        "dashboard": {"name": "Coxswain", "grid": [[{"chart": "Chart A", "width": 12}]]},
    }
    ops = bootstrap.plan(specs, {}, PRESENT)
    board_ops = [o for o in ops if o.kind == "dashboard"]
    assert [o.name for o in board_ops] == ["Coxswain"]


def test_names_of_names_all_three_dashboards():
    dashboards = [
        {"name": "Coxswain", "grid": [[{"chart": "Chart A", "width": 12}]]},
        {"name": "Fleet", "grid": [[{"chart": "Chart A", "width": 12}]]},
        {"name": "Chair", "grid": [[{"chart": "Chart A", "width": 12}]]},
    ]
    assert bootstrap.names_of(_specs(dashboards))["dashboard"] == {"Coxswain", "Fleet", "Chair"}
