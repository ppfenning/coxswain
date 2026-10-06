import importlib.util
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load("superset_check_crew", DEPLOY / "check.py")
bootstrap = check.load_bootstrap()
SPECS = bootstrap.load_specs(DEPLOY / "dashboards")
CREW = [c["chart"] for row in next(d for d in SPECS["dashboards"] if d["name"] == "Crew")["grid"] for c in row]
CHARTS = [{"id": i, "slice_name": name} for i, name in enumerate(CREW, 1)]
WITH_ROWS = {"result": [{"data": [{"day": "2026-10-05", "cost": 1.5}]}]}
NO_ROWS = {"result": [{"data": []}]}


def test_every_chart_on_the_crew_dashboard_is_visited_by_the_row_check():
    visited = []

    def fetch(chart):
        visited.append(chart["slice_name"])
        return WITH_ROWS

    lines, code = check.report(check.chart_answers(CHARTS, fetch))
    assert len(CREW) == 11
    assert visited == CREW
    assert lines == [f"{name}: 1 rows" for name in CREW] and code == 0


def test_a_crew_chart_returning_zero_rows_exits_nonzero_and_names_the_chart():
    name = "Crew cost per day by role"
    lines, code = check.report(check.chart_answers(CHARTS, lambda chart: NO_ROWS if chart["slice_name"] == name else WITH_ROWS))
    assert code == 1
    assert [line for line in lines if "0 rows" in line] == [f"{name}: 0 rows"]


def test_a_crew_chart_returning_rows_passes():
    name = "Cost per approved task per role"
    lines, code = check.report(check.chart_answers([c for c in CHARTS if c["slice_name"] == name], lambda chart: WITH_ROWS))
    assert (lines, code) == ([f"{name}: 1 rows"], 0)
