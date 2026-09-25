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
_check_spec = importlib.util.spec_from_file_location("superset_check", DEPLOY / "check.py")
check = importlib.util.module_from_spec(_check_spec)
_check_spec.loader.exec_module(check)

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


# Written by hand from Superset 4.1's frontend, not from queries_of: the x axis is the BASE_AXIS column
# getXAxisColumn builds, carrying timeGrain only when time_grain_sqla is set; group-by columns follow.
SUM_COST = {"expressionType": "SIMPLE", "column": {"column_name": "cost_usd"}, "aggregate": "SUM", "label": "cost_usd"}
TOOL_USES = {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "tool_uses"}
EXPECTED_QUERIES = {
    "Cost per day by model alias": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "at", "sqlExpression": "at", "timeGrain": "P1D"}, "model_alias"],
            "metrics": [SUM_COST],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Cost by role, last 7 days": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "role", "sqlExpression": "role"}],
            "metrics": [SUM_COST],
            "orderby": [],
            "row_limit": 10000,
            "filters": [{"col": "at", "op": "TEMPORAL_RANGE", "val": "Last week"}],
            "extras": {},
        }
    ],
    "Busy lanes per hour": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "hour_start", "sqlExpression": "hour_start", "timeGrain": "PT1H"}],
            "metrics": [{"expressionType": "SIMPLE", "column": {"column_name": "busy_lanes"}, "aggregate": "MAX", "label": "busy_lanes"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "PT1H"},
        }
    ],
    "Runs per day": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "launched_at", "sqlExpression": "launched_at", "timeGrain": "P1D"}],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "runs"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Quarantined attempts per day": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "at", "sqlExpression": "at", "timeGrain": "P1D"}],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "quarantined"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [{"col": "kind", "op": "IN", "val": ["refused", "unverified", "infra", "dropped"]}],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Tool uses by name, top 15": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "tool_name", "sqlExpression": "tool_name"}],
            "metrics": [TOOL_USES],
            "orderby": [[TOOL_USES, False]],
            "row_limit": 15,
            "filters": [],
            "extras": {},
        }
    ],
}


def test_each_chart_queries_its_axis_group_by_metrics_filters_and_grain():
    assert {c["name"]: bootstrap.queries_of(c["params"]) for c in SPECS["charts"]} == EXPECTED_QUERIES


def test_chart_body_carries_a_query_context_for_the_resolved_dataset():
    ops = bootstrap.plan(SPECS, {}, ALL_SOURCES)
    ids = {"database": {"coxswain": 1}, "dataset": {d["name"]: 10 + i for i, d in enumerate(SPECS["datasets"])}}
    (chart,) = [o for o in ops if o.name == "Runs per day"]
    assert json.loads(bootstrap.body(chart, ids)["query_context"]) == {
        "datasource": {"id": ids["dataset"]["runs"], "type": "table"},
        "force": False,
        "queries": EXPECTED_QUERIES["Runs per day"],
        "result_format": "json",
        "result_type": "full",
    }


def _api_listing():
    """Superset 4.1's detail answers for objects the specs made, built from SPECS and EXPECTED_QUERIES, never from `plan`.

    The database is the /connection answer. Charts carry no datasource_id; params and query_context are strings.
    """
    dataset_ids = {d["name"]: 10 + i for i, d in enumerate(SPECS["datasets"])}
    chart_ids = {c["name"]: 20 + i for i, c in enumerate(SPECS["charts"])}
    database = {
        "id": 1,
        "database_name": "coxswain",
        "sqlalchemy_uri": SPECS["database"]["sqlalchemy_uri"],
        "extra": '{"allows_virtual_table_explore": true}',
        "expose_in_sqllab": True,
        "backend": "duckdb",
    }
    datasets = {
        d["name"]: {"id": dataset_ids[d["name"]], "table_name": d["name"], "database": {"id": 1, "database_name": "coxswain", "backend": "duckdb"}, "sql": d["sql"]}
        for d in SPECS["datasets"]
    }
    charts = {
        c["name"]: {
            "id": chart_ids[c["name"]],
            "slice_name": c["name"],
            "viz_type": c["viz_type"],
            "params": json.dumps({"slice_id": chart_ids[c["name"]], **c["params"], "viz_type": c["viz_type"], "datasource": f"{dataset_ids[c['dataset']]}__table"}),
            "query_context": json.dumps(
                {"datasource": {"id": dataset_ids[c["dataset"]], "type": "table"}, "force": False, "queries": EXPECTED_QUERIES[c["name"]], "result_format": "json", "result_type": "full"}
            ),
            "dashboards": [{"id": 1, "dashboard_title": "Coxswain"}],
        }
        for c in SPECS["charts"]
    }
    dashboard = {"id": 1, "dashboard_title": "Coxswain", "position_json": json.dumps(bootstrap.position(SPECS["dashboard"]["grid"], chart_ids, "Coxswain"))}
    return {"database": {"coxswain": database}, "dataset": datasets, "chart": charts, "dashboard": {"Coxswain": dashboard}}, {i: n for n, i in dataset_ids.items()}


def _existing(listing, dataset_names):
    return {k: {n: bootstrap.normalize(k, d, dataset_names) for n, d in named.items()} for k, named in listing.items()}


def test_plan_on_a_listing_shaped_like_the_api_changes_nothing():
    listing, dataset_names = _api_listing()
    ops = bootstrap.plan(SPECS, _existing(listing, dataset_names), ALL_SOURCES)
    assert [(o.action, o.kind, o.name) for o in ops if o.action != "unchanged"] == []


def test_the_plain_database_show_answer_lacks_the_uri_so_the_database_is_read_from_connection():
    listing, dataset_names = _api_listing()
    show = {k: v for k, v in listing["database"]["coxswain"].items() if k != "sqlalchemy_uri"}
    ops = bootstrap.plan(SPECS, _existing({**listing, "database": {"coxswain": show}}, dataset_names), ALL_SOURCES)
    assert [(o.action, o.kind) for o in ops if o.action != "unchanged"] == [("update", "database")]
    assert (bootstrap.detail_path("database", 1), bootstrap.detail_path("chart", 20)) == ("/api/v1/database/1/connection", "/api/v1/chart/20")


def test_a_masked_password_reads_unchanged_and_the_real_one_is_still_sent():
    specs = {**SPECS, "database": {"name": "coxswain", "sqlalchemy_uri": "postgresql://u:secret@h/db"}}
    existing = {"database": {"coxswain": bootstrap.normalize("database", {"sqlalchemy_uri": "postgresql://u:XXXXXXXXXX@h/db"}, {})}}
    (op,) = [o for o in bootstrap.plan(specs, existing, ALL_SOURCES) if o.kind == "database"]
    assert (op.action, bootstrap.body(op, {})["sqlalchemy_uri"]) == ("unchanged", "postgresql://u:secret@h/db")


def test_a_chart_saved_without_a_query_context_is_updated_and_nothing_else():
    listing, dataset_names = _api_listing()
    runs = {**listing["chart"]["Runs per day"], "query_context": None}
    ops = bootstrap.plan(SPECS, _existing({**listing, "chart": {**listing["chart"], "Runs per day": runs}}, dataset_names), ALL_SOURCES)
    assert [(o.action, o.kind, o.name) for o in ops if o.action != "unchanged"] == [("update", "chart", "Runs per day")]


def test_check_report_prints_rows_and_errors_and_exits_1_on_any_error():
    answers = {
        "Runs per day": {"result": [{"data": [{"runs": 1}, {"runs": 2}]}]},
        "Empty": {"result": [{"data": []}]},
        "No context": {"message": "Chart has no query context saved. Please save the chart again."},
        "Bad query": {"result": [{"error": "Catalog Error"}]},
    }
    assert check.report(answers) == (
        [
            "Runs per day: 2 rows",
            "Empty: 0 rows",
            "No context: ERROR Chart has no query context saved. Please save the chart again.",
            "Bad query: ERROR Catalog Error",
        ],
        1,
    )
    assert check.report({"a: ERROR b": {"result": [{"data": [{}]}]}}) == (["a: ERROR b: 1 rows"], 0)
