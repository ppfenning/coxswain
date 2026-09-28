import datetime
import importlib.util
import json
import re
import urllib.error
from collections import Counter
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"
_spec = importlib.util.spec_from_file_location("superset_bootstrap", DEPLOY / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)
_check_spec = importlib.util.spec_from_file_location("superset_check", DEPLOY / "check.py")
check = importlib.util.module_from_spec(_check_spec)
_check_spec.loader.exec_module(check)

SPECS = bootstrap.load_specs(DEPLOY / "dashboards")
ALL_SOURCES = frozenset({"sqlite", "parquet-traces", "land-log", "chair-store", "hosts-table"})
CHAIR_DATASETS = ["chair_actions_by_hour", "chair_lanes_by_host_hour", "chair_spend_by_day", "chair_needs_backlog", "chair_status"]
FLEET_DATASETS = ["fleet_host_state", "fleet_needs_chair_by_cause", "fleet_weekly_spend"]
# fleet_needs_chair_by_cause and fleet_weekly_spend read chair_actions/chair_ticks, so chair-store gates them too.
FLEET_CHAIR_DATASETS = [*CHAIR_DATASETS, "fleet_needs_chair_by_cause", "fleet_weekly_spend"]
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
        sql = bootstrap.expand_store(d["sql"], None)
        assert isinstance(sql, str), d["name"]
        paths = LITERAL_READS.findall(sql)
        assert paths, d["name"]
        assert len(READS.findall(sql)) == len(paths), f"{d['name']} reads a source that is not a literal path"
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


def test_the_cause_chart_reuses_the_quarantine_kinds():
    (quarantine,) = [c for c in SPECS["charts"] if "counts_kinds" in c]
    (cause,) = [c for c in SPECS["charts"] if c["name"] == "Quarantines by cause, last 14 days"]
    (kind_filter,) = [f for f in cause["params"]["adhoc_filters"] if f["subject"] == "kind"]
    assert kind_filter["comparator"] == quarantine["counts_kinds"]


def test_dashboard_rows_fill_twelve_columns():
    assert all(sum(c["width"] for c in row) == 12 for row in SPECS["dashboard"]["grid"])


def test_plan_on_an_empty_server_creates_everything_in_order():
    ops = bootstrap.plan(SPECS, {}, ALL_SOURCES)
    assert {o.action for o in ops} == {"create"}
    kinds = [o.kind for o in ops]
    assert kinds == sorted(kinds, key=bootstrap.KINDS.index)
    assert Counter(kinds) == {"database": 1, "dataset": 18, "chart": 17, "dashboard": 1}


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
    ops = bootstrap.plan(SPECS, {}, frozenset({"sqlite", "land-log", "chair-store", "hosts-table"}))
    lines = [bootstrap.line(o) for o in ops if o.action == "skipped"]
    assert lines[0] == "skipped: traces (no Parquet traces yet)"
    assert lines[1] == "skipped: Tool uses by name, top 15 (dataset traces skipped)"
    assert len(lines) == 2
    (board,) = [o for o in ops if o.kind == "dashboard"]
    assert "Tool uses by name, top 15" not in json.dumps(board.payload)
    assert Counter(o.kind for o in ops if o.action == "create") == {"database": 1, "dataset": 17, "chart": 16, "dashboard": 1}


def test_plan_skips_the_land_log_datasets_and_their_charts_while_no_land_log_exists():
    ops = bootstrap.plan(SPECS, {}, frozenset({"sqlite", "parquet-traces", "chair-store", "hosts-table"}))
    assert [bootstrap.line(o) for o in ops if o.action == "skipped"] == [
        "skipped: daily_efficiency (needs land.jsonl and cox.db)",
        "skipped: landed_tasks (needs land.jsonl and cox.db)",
        "skipped: task_outcomes (needs land.jsonl and cox.db)",
        "skipped: Cost per landed task, by day (dataset daily_efficiency skipped)",
        "skipped: Landed tasks per day (dataset daily_efficiency skipped)",
        "skipped: First-try build rate, by day (dataset landed_tasks skipped)",
        "skipped: Lead time, launch to land (median hours), by day (dataset landed_tasks skipped)",
        "skipped: Spend by task outcome, last 14 days (dataset task_outcomes skipped)",
    ]
    (board,) = [o for o in ops if o.kind == "dashboard"]
    assert "Landed tasks per day" not in json.dumps(board.payload)
    assert "Cost per turn by model, by day" in json.dumps(board.payload)


def test_present_sources_counts_the_land_log_only_beside_a_cox_db(tmp_path):
    (tmp_path / "land.jsonl").write_text("")
    assert bootstrap.present_sources(tmp_path) == frozenset()
    (tmp_path / "cox.db").write_text("")
    assert bootstrap.present_sources(tmp_path) == frozenset({"sqlite", "land-log"})


def test_plan_without_chair_store_skips_the_chair_gated_datasets_and_keeps_the_others():
    ops = bootstrap.plan(SPECS, {}, ALL_SOURCES - {"chair-store"})
    assert [bootstrap.line(o) for o in ops if o.action == "skipped"] == [f"skipped: {n} (no chair tables in the store yet)" for n in FLEET_CHAIR_DATASETS]
    assert [o.name for o in ops if o.kind == "dataset" and o.action == "create"] == [d["name"] for d in SPECS["datasets"] if d["name"] not in FLEET_CHAIR_DATASETS]
    assert not [c for c in SPECS["charts"] if c["dataset"] in FLEET_CHAIR_DATASETS]
    assert Counter(o.kind for o in ops if o.action == "create") == {"database": 1, "dataset": 11, "chart": 17, "dashboard": 1}


def test_a_store_without_chair_store_plans_the_existing_datasets_as_before():
    with_chair = bootstrap.plan(SPECS, {}, ALL_SOURCES)
    without = bootstrap.plan(SPECS, {}, ALL_SOURCES - {"chair-store"})
    old = [o for o in with_chair if o.name not in FLEET_CHAIR_DATASETS]
    assert [o for o in without if o.action != "skipped"] == old
    assert bootstrap.present_sources(Path("/nonexistent")) == frozenset()


def test_no_chair_dataset_sql_names_a_log_file_or_a_scan():
    sqls = {d["name"]: d["sql"] for d in SPECS["datasets"] if d["name"] in CHAIR_DATASETS}
    postgres = {n: bootstrap.expand_store(sql, "postgresql://u@h/db") for n, sql in sqls.items()}
    assert sorted(sqls) == sorted(CHAIR_DATASETS)
    assert not [n for n, sql in sqls.items() if ".jsonl" in sql or "sqlite_scan" in sql or READS.findall(sql)]
    assert not [n for n, sql in postgres.items() if ".jsonl" in sql or "sqlite_scan" in sql or "store.public." not in sql]
    assert all(d["requires"] == "chair-store" for d in SPECS["datasets"] if d["name"] in CHAIR_DATASETS)


class _SqlLab:
    """SQL Lab over a store holding `tables`. A select on any other table answers 400, as a query error does."""

    def __init__(self, tables, code=400):
        self.tables, self.code, self.sent = tables, code, []

    def call(self, method, path, data=None):
        self.sent.append(data["sql"])
        if re.search(r"'(\w+)'\) LIMIT 0$", data["sql"])[1] not in self.tables:
            raise urllib.error.HTTPError(path, self.code, "error", {}, None)
        return {"data": []}


def test_the_chair_probe_selects_limit_0_from_every_table_it_reads_and_needs_all():
    full = _SqlLab({"chair_actions", "chair_ticks", "leases", "runs"})
    assert bootstrap.chair_tables_readable(full, 1, None) is True
    assert full.sent[0] == "SELECT ts, kind, initiative, task, status, reason FROM sqlite_scan('/data/runs/cox.db', 'chair_actions') LIMIT 0"
    assert full.sent[2] == "SELECT name, holder, host, epoch, heartbeat_at FROM sqlite_scan('/data/runs/cox.db', 'leases') LIMIT 0"
    assert bootstrap.chair_tables_readable(_SqlLab({"chair_actions", "leases", "runs"}), 1, None) is False
    assert bootstrap.present_sources(Path("/nonexistent"), True) == frozenset({"chair-store"})


def test_the_chair_probe_raises_on_an_error_that_is_not_a_query_error():
    with pytest.raises(urllib.error.HTTPError) as raised:
        bootstrap.chair_tables_readable(_SqlLab(set(), code=401), 1, None)
    assert raised.value.code == 401


def test_the_hosts_probe_selects_limit_0_from_the_hosts_table():
    full = _SqlLab({"hosts"})
    assert bootstrap.hosts_table_readable(full, 1, None) is True
    assert full.sent == ["SELECT host, state, last_login_check_at FROM sqlite_scan('/data/runs/cox.db', 'hosts') LIMIT 0"]
    assert bootstrap.hosts_table_readable(_SqlLab({"chair_actions"}), 1, None) is False
    assert bootstrap.present_sources(Path("/nonexistent"), hosts_table=True) == frozenset({"hosts-table"})


def test_plan_without_hosts_table_skips_fleet_host_state_and_keeps_the_other_new_datasets_unchanged():
    ops = bootstrap.plan(SPECS, {}, ALL_SOURCES - {"hosts-table"})
    assert [bootstrap.line(o) for o in ops if o.action == "skipped"] == ["skipped: fleet_host_state (no hosts table in the store yet)"]
    assert [o.name for o in ops if o.kind == "dataset" and o.action == "create" and o.name in FLEET_DATASETS] == [
        "fleet_needs_chair_by_cause",
        "fleet_weekly_spend",
    ]


CHAIR_STORE = """
SET TimeZone = 'UTC';
CREATE TABLE node_calls AS SELECT * FROM (VALUES ('2026-09-20T10:00:00Z', 3.0)) AS t(ts, cost_usd);
CREATE TABLE chair_actions AS SELECT * FROM (VALUES
  ('2026-09-21T09:00:00Z', 3, 'needs_chair', 'i', 'a', 'ok', 'review'),
  ('2026-09-21T09:00:00Z', 3, 'needs_chair', 'i', 'b', 'ok', 'conflict'),
  ('2026-09-21T10:00:00Z', 3, 'land', 'i', 'a', 'ok', ''),
  ('2026-09-21T10:00:00Z', 3, 'land', 'i', 'b', 'failed', 'merge')
) AS t(ts, epoch, kind, initiative, task, status, reason);
CREATE TABLE chair_ticks AS SELECT * FROM (VALUES
  ('2026-09-21T10:00:00Z', 'me', 'box1', 3, 8, 0.4),
  ('2026-09-21T11:00:00Z', 'me', 'box1', 3, 8, 0.5)
) AS t(ts, holder, host, epoch, max_in_flight, weekly_fraction);
CREATE TABLE leases AS SELECT * FROM (VALUES
  ('chair', 'me', 'box1', 3, '2026-09-21T11:00:00Z', '2026-09-21T11:05:00Z')
) AS t(name, holder, host, epoch, heartbeat_at, expires_at);
CREATE TABLE runs AS SELECT '' AS host, CAST(now() - INTERVAL 2 HOUR AS VARCHAR) AS launched_at, CAST(NULL AS VARCHAR) AS ended_at;
"""


def _chair_rows(name):
    """The dataset's rows over CHAIR_STORE, with each placeholder read as a plain table."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute(CHAIR_STORE)
    (sql,) = [d["sql"] for d in SPECS["datasets"] if d["name"] == name]
    return con.execute(bootstrap.STORE_FORM.sub(lambda m: m[1], sql)).fetchall()


def test_the_chair_sql_runs_over_literal_rows_in_duckdb():
    assert _chair_rows("chair_spend_by_day") == [
        (datetime.date(2026, 9, 20), 3.0, 0, None, None),
        (datetime.date(2026, 9, 21), 0.0, 1, 0.0, 0.0),
    ]
    assert [(r[0], r[1]) for r in _chair_rows("chair_needs_backlog")] == [("i", "b")]
    (status,) = _chair_rows("chair_status")
    assert status[:3] == ("me", "box1", 3) and status[3] > 0 and status[4] == 0.5
    assert {(r[1], r[3]) for r in _chair_rows("chair_lanes_by_host_hour")} == {("local", 8)}
    assert sorted((r[1], r[2]) for r in _chair_rows("chair_actions_by_hour")) == [("land", 2), ("needs_chair", 2)]


def test_the_chair_backlog_flagged_at_is_a_naive_utc_datetime():
    (row,) = [r for r in _chair_rows("chair_needs_backlog") if r[1] == "b"]
    assert row[3] == datetime.datetime.fromisoformat("2026-09-21T09:00:00")
    assert row[3].tzinfo is None


FLEET_STORE = """
SET TimeZone = 'UTC';
CREATE TABLE hosts AS SELECT * FROM (VALUES
  ('box1', 'active', '2026-09-21T09:00:00Z'),
  ('box2', 'quarantined', '2026-09-19T08:00:00Z')
) AS t(host, state, last_login_check_at);
CREATE TABLE chair_actions AS SELECT * FROM (VALUES
  ('2026-09-21T09:00:00Z', 3, 'needs_chair', 'i', 'a', 'ok', 'review'),
  ('2026-09-21T09:00:00Z', 3, 'needs_chair', 'i', 'b', 'ok', 'conflict'),
  ('2026-09-21T10:00:00Z', 3, 'land', 'i', 'a', 'ok', ''),
  ('2026-09-21T10:00:00Z', 3, 'land', 'i', 'b', 'failed', 'merge')
) AS t(ts, epoch, kind, initiative, task, status, reason);
CREATE TABLE chair_ticks AS SELECT ts, holder, host, epoch, max_in_flight, CAST(weekly_fraction AS DOUBLE) AS weekly_fraction FROM (VALUES
  ('2026-09-14T10:00:00Z', 'me', 'box1', 3, 8, 0.9),
  ('2026-09-15T10:00:00Z', 'me', 'box1', 3, 8, 0.1),
  ('2026-09-16T10:00:00Z', 'me', 'box1', 3, 8, 0.3)
) AS t(ts, holder, host, epoch, max_in_flight, weekly_fraction);
CREATE TABLE node_calls AS SELECT * FROM (VALUES
  ('2026-09-14T11:00:00Z', 2.0),
  ('2026-09-15T11:00:00Z', 3.0),
  ('2026-09-16T11:00:00Z', 1.0)
) AS t(ts, cost_usd);
"""


def _fleet_rows(name):
    """The dataset's rows over FLEET_STORE, with each placeholder read as a plain table."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute(FLEET_STORE)
    (sql,) = [d["sql"] for d in SPECS["datasets"] if d["name"] == name]
    return con.execute(bootstrap.STORE_FORM.sub(lambda m: m[1], sql)).fetchall()


def test_the_fleet_sql_runs_over_literal_rows_in_duckdb():
    assert _fleet_rows("fleet_host_state") == [
        ("box1", "active", datetime.datetime.fromisoformat("2026-09-21T09:00:00")),
        ("box2", "quarantined", datetime.datetime.fromisoformat("2026-09-19T08:00:00")),
    ]
    assert all(r[2].tzinfo is None for r in _fleet_rows("fleet_host_state"))
    assert [(r[0], r[1]) for r in _fleet_rows("fleet_needs_chair_by_cause")] == [("conflict", 1)]
    assert _fleet_rows("fleet_weekly_spend") == [
        (datetime.date(2026, 9, 15), 3.0, 3.0, 0.1),
        (datetime.date(2026, 9, 16), 1.0, 4.0, 0.3),
    ]
    assert not [d for d in SPECS["datasets"] if d["name"] == "fleet_drafts_waiting_approval"]


def test_no_fleet_dataset_sql_names_a_log_file_or_a_scan():
    sqls = {d["name"]: d["sql"] for d in SPECS["datasets"] if d["name"] in FLEET_DATASETS}
    postgres = {n: bootstrap.expand_store(sql, "postgresql://u@h/db") for n, sql in sqls.items()}
    assert sorted(sqls) == sorted(FLEET_DATASETS)
    assert not [n for n, sql in sqls.items() if ".jsonl" in sql or "sqlite_scan" in sql or READS.findall(sql)]
    assert not [n for n, sql in postgres.items() if ".jsonl" in sql or "sqlite_scan" in sql or "store.public." not in sql]


def test_a_later_run_adds_traces_and_updates_the_dashboard():
    first = bootstrap.plan(SPECS, {}, frozenset({"sqlite", "land-log", "chair-store", "hosts-table"}))
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
DAY_AXIS = {"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "day", "sqlExpression": "day", "timeGrain": "P1D"}
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
    "Quarantines by cause, last 14 days": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "cause", "sqlExpression": "cause"}],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "quarantined"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [
                {"col": "kind", "op": "IN", "val": ["refused", "unverified", "infra", "dropped"]},
                {"col": "at", "op": "TEMPORAL_RANGE", "val": "Last 2 weeks"},
            ],
            "extras": {},
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
    "Cost per turn by model, by day": [
        {
            "columns": [DAY_AXIS, "model_alias"],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "SUM(cost_usd) / NULLIF(SUM(turns), 0)", "label": "cost_per_turn"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Cache-read share by model, by day": [
        {
            "columns": [DAY_AXIS, "model_alias"],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "SUM(cache_read_share * turns) / NULLIF(SUM(turns), 0)", "label": "cache_read_share"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Cost per landed task, by day": [
        {
            "columns": [DAY_AXIS],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "SUM(cost_usd) / NULLIF(SUM(landed), 0)", "label": "cost_per_landed"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Landed tasks per day": [
        {
            "columns": [DAY_AXIS],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "SUM(landed)", "label": "landed"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "First-try build rate, by day": [
        {
            "columns": [DAY_AXIS],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "AVG(CAST(first_try AS INTEGER))", "label": "first_try_rate"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Lead time, launch to land (median hours), by day": [
        {
            "columns": [DAY_AXIS],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "MEDIAN(lead_hours)", "label": "lead_hours"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Spend by task outcome, last 14 days": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "outcome", "sqlExpression": "outcome"}],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "SUM(cost_usd)", "label": "cost_usd"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [{"col": "last_call", "op": "TEMPORAL_RANGE", "val": "Last 2 weeks"}],
            "extras": {},
        }
    ],
    "Reviewer agreement": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "charter_verdict", "sqlExpression": "charter_verdict"}, "adversary_verdict"],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "tasks"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {},
        }
    ],
    "Arbiter decisions": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "arbiter_verdict", "sqlExpression": "arbiter_verdict"}],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "tasks"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {},
        }
    ],
    "Build attempts per task": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "build_attempts", "sqlExpression": "build_attempts"}],
            "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "tasks"}],
            "orderby": [],
            "row_limit": 10000,
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
        d["name"]: {"id": dataset_ids[d["name"]], "table_name": d["name"], "database": {"id": 1, "database_name": "coxswain", "backend": "duckdb"}, "sql": bootstrap.expand_store(d["sql"], None)}
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
