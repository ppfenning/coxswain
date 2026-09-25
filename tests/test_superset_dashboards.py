import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"
_spec = importlib.util.spec_from_file_location("superset_bootstrap", DEPLOY / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)

SPECS = bootstrap.load_specs(DEPLOY / "dashboards")
ALL_SOURCES = frozenset({"sqlite", "parquet-traces"})
READS = re.compile(r"\b(?:read_\w+|\w+_scan)\s*\(")
LITERAL_READS = re.compile(r"\b(?:read_\w+|\w+_scan)\s*\(\s*'([^']*)'")


def _server(ops):
    """What a server that already holds every planned object would list."""
    return {kind: {o.name: o.payload for o in ops if o.kind == kind and o.action != "skipped"} for kind in bootstrap.KINDS}


def test_every_chart_names_a_dataset_that_exists():
    datasets = {d["name"] for d in SPECS["datasets"]}
    assert SPECS["charts"]
    assert {c["dataset"] for c in SPECS["charts"]} <= datasets


def test_every_dataset_sql_reads_only_under_data_runs():
    for d in SPECS["datasets"]:
        paths = LITERAL_READS.findall(d["sql"])
        assert paths, d["name"]
        assert len(READS.findall(d["sql"])) == len(paths), f"{d['name']} reads a source that is not a literal path"
        assert all(p.startswith("/data/runs/") for p in paths), d["name"]


def test_names_are_unique_within_each_kind():
    cells = [c["chart"] for row in SPECS["dashboard"]["grid"] for c in row]
    for names in ([d["name"] for d in SPECS["datasets"]], [c["name"] for c in SPECS["charts"]], cells):
        assert not [n for n, k in Counter(names).items() if k > 1]
    assert sorted(cells) == sorted(c["name"] for c in SPECS["charts"])


def test_every_dataset_names_the_one_database_and_a_known_source():
    assert all(d["database"] == SPECS["database"]["name"] for d in SPECS["datasets"])
    assert all(d["requires"] in bootstrap.SOURCE_NOTES for d in SPECS["datasets"])


def test_quarantine_chart_counts_exactly_the_kinds_it_states():
    (chart,) = [c for c in SPECS["charts"] if "counts_kinds" in c]
    (kind_filter,) = [f for f in chart["params"]["adhoc_filters"] if f["subject"] == "kind"]
    assert chart["counts_kinds"]
    assert kind_filter["comparator"] == chart["counts_kinds"]


def test_dashboard_rows_fill_twelve_columns():
    assert all(sum(c["width"] for c in row) == 12 for row in SPECS["dashboard"]["grid"])


def test_plan_on_an_empty_server_creates_everything_in_order():
    ops = bootstrap.plan(SPECS, {}, ALL_SOURCES)
    assert {o.action for o in ops} == {"create"}
    kinds = [o.kind for o in ops]
    assert kinds == sorted(kinds, key=bootstrap.KINDS.index)
    assert Counter(kinds) == {"database": 1, "dataset": 5, "chart": 6, "dashboard": 1}


def test_plan_is_all_unchanged_when_the_server_matches():
    ops = bootstrap.plan(SPECS, _server(bootstrap.plan(SPECS, {}, ALL_SOURCES)), ALL_SOURCES)
    assert {o.action for o in ops} == {"unchanged"}
    assert bootstrap.line(ops[0]) == "unchanged: database coxswain"


def test_plan_treats_keys_the_server_added_to_a_chart_as_unchanged():
    server = _server(bootstrap.plan(SPECS, {}, ALL_SOURCES))
    server["chart"]["Runs per day"] = {**server["chart"]["Runs per day"], "params": {**SPECS["charts"][3]["params"], "datasource": "3__table"}}
    ops = bootstrap.plan(SPECS, server, ALL_SOURCES)
    assert {o.action for o in ops} == {"unchanged"}


def test_plan_updates_a_dataset_whose_sql_changed_and_only_that_one():
    server = _server(bootstrap.plan(SPECS, {}, ALL_SOURCES))
    server["dataset"]["runs"] = {**server["dataset"]["runs"], "sql": "SELECT 1"}
    ops = bootstrap.plan(SPECS, server, ALL_SOURCES)
    assert [(o.kind, o.name) for o in ops if o.action == "update"] == [("dataset", "runs")]


def test_plan_skips_traces_and_its_chart_while_no_parquet_exists():
    ops = bootstrap.plan(SPECS, {}, frozenset({"sqlite"}))
    lines = [bootstrap.line(o) for o in ops if o.action == "skipped"]
    assert lines[0] == "skipped: traces (no Parquet traces yet)"
    assert lines[1] == "skipped: Tool uses by name, top 15 (dataset traces skipped)"
    assert len(lines) == 2
    (board,) = [o for o in ops if o.kind == "dashboard"]
    assert "Tool uses by name, top 15" not in json.dumps(board.payload)
    assert Counter(o.kind for o in ops if o.action == "create") == {"database": 1, "dataset": 4, "chart": 5, "dashboard": 1}


def test_a_later_run_adds_traces_and_updates_the_dashboard():
    first = bootstrap.plan(SPECS, {}, frozenset({"sqlite"}))
    ops = bootstrap.plan(SPECS, _server(first), ALL_SOURCES)
    assert [(o.action, o.kind, o.name) for o in ops if o.action != "unchanged"] == [
        ("create", "dataset", "traces"),
        ("create", "chart", "Tool uses by name, top 15"),
        ("update", "dashboard", "Coxswain"),
    ]


def test_position_json_reads_back_as_the_grid_it_was_written_from():
    grid = SPECS["dashboard"]["grid"]
    ids = {c["chart"]: i for i, row in enumerate(grid) for c in row}
    assert bootstrap.grid_of(bootstrap.position(grid, ids, "Coxswain")) == grid


def test_body_resolves_names_to_server_ids():
    ops = bootstrap.plan(SPECS, {}, ALL_SOURCES)
    ids = {"database": {"coxswain": 1}, "dataset": {d["name"]: 10 + i for i, d in enumerate(SPECS["datasets"])}}
    (chart,) = [o for o in ops if o.name == "Runs per day"]
    sent = bootstrap.body(chart, ids)
    assert sent["datasource_id"] == ids["dataset"]["runs"]
    assert json.loads(sent["params"])["datasource"] == f"{ids['dataset']['runs']}__table"
    (dataset,) = [o for o in ops if o.name == "runs"]
    assert bootstrap.body(dataset, ids)["database"] == 1


def test_specs_are_plain_yaml_files_in_the_dashboards_directory():
    assert sorted(p.name for p in (DEPLOY / "dashboards").glob("*.yaml")) == ["charts.yaml", "dashboard.yaml", "database.yaml", "datasets.yaml"]
    assert all(isinstance(yaml.safe_load(p.read_text()), dict) for p in (DEPLOY / "dashboards").glob("*.yaml"))
