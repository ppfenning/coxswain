import datetime
import importlib.util
import io
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
ALL_SOURCES = frozenset({"store", "lake"})
CHAIR_DATASETS = ["chair_actions_by_hour", "chair_lanes_by_host_hour", "chair_spend_by_day", "chair_needs_backlog", "chair_status"]
FLEET_DATASETS = ["fleet_host_state", "fleet_needs_chair_by_cause", "fleet_weekly_spend"]
# Every dataset gated by the single `store` requirement, in datasets.yaml order.
STORE_GATED_DATASETS = [*CHAIR_DATASETS, *FLEET_DATASETS]
# The ten history datasets, all gated by the single `lake` requirement, in datasets.yaml order.
LAKE_DATASETS = ["calls", "runs", "attempts", "lanes_by_hour", "traces", "calls_by_day", "daily_efficiency", "landed_tasks", "task_outcomes", "task_verdicts"]
# The Fleet dashboard's charts: five, not the brief's six, since fleet_drafts_waiting_approval
# has no dataset yet. Each pairs with the chart it gates when store is absent.
FLEET_CHART_NAMES = [
    "Lanes per host over time against capacity",
    "Host state and last login",
    "Chair holder, epoch and beat age",
    "Needs-chair items by cause",
    "Weekly spend against the ceiling",
]
CHAIR_CHART_NAMES = ["Chair actions per hour by kind", "Spend per landed task per day", "Needs-chair backlog"]
# Every chart gated by `store`, paired with the dataset that gates it, in charts.yaml order.
STORE_GATED_CHARTS = [
    ("Lanes per host over time against capacity", "chair_lanes_by_host_hour"),
    ("Host state and last login", "fleet_host_state"),
    ("Chair holder, epoch and beat age", "chair_status"),
    ("Needs-chair items by cause", "fleet_needs_chair_by_cause"),
    ("Weekly spend against the ceiling", "fleet_weekly_spend"),
    ("Chair actions per hour by kind", "chair_actions_by_hour"),
    ("Spend per landed task per day", "chair_spend_by_day"),
    ("Needs-chair backlog", "chair_needs_backlog"),
]
READS = re.compile(r"\b(?:read_\w+|\w+_scan)\s*\(")
LITERAL_READS = re.compile(r"\b(?:read_\w+|\w+_scan)\s*\(\s*'([^']*)'")


def _dashboard(name):
    (dash,) = [d for d in SPECS["dashboards"] if d["name"] == name]
    return dash


def _server(ops):
    """What a server that already holds every planned object would list."""
    return {kind: {o.name: o.payload for o in ops if o.kind == kind and o.action != "skipped"} for kind in bootstrap.KINDS}


def test_every_chart_names_a_dataset_that_exists():
    datasets = {d["name"] for d in SPECS["datasets"]}
    assert SPECS["charts"]
    assert {c["dataset"] for c in SPECS["charts"]} <= datasets


def test_the_fleet_dashboard_names_its_charts_and_each_names_a_dataset_that_exists():
    cells = [c["chart"] for row in _dashboard("Fleet")["grid"] for c in row]
    assert sorted(cells) == sorted(FLEET_CHART_NAMES)
    datasets = {d["name"] for d in SPECS["datasets"]}
    dataset_of = {c["name"]: c["dataset"] for c in SPECS["charts"]}
    assert all(dataset_of[name] in datasets for name in FLEET_CHART_NAMES)


def test_the_chair_dashboard_names_its_charts_and_each_names_a_dataset_that_exists():
    cells = [c["chart"] for row in _dashboard("Chair")["grid"] for c in row]
    assert sorted(cells) == sorted(CHAIR_CHART_NAMES)
    datasets = {d["name"] for d in SPECS["datasets"]}
    dataset_of = {c["name"]: c["dataset"] for c in SPECS["charts"]}
    assert all(dataset_of[name] in datasets for name in CHAIR_CHART_NAMES)


def test_the_coxswain_dashboard_in_dashboards_is_unchanged():
    # A literal copy of the grid before the Fleet and Chair dashboards were added. The yaml anchor
    # makes SPECS["dashboard"] and this entry one object, so only a literal can catch a change.
    assert _dashboard("Coxswain")["grid"] == [
        [{"chart": "Cost per turn by model, by day", "width": 6}, {"chart": "Cache-read share by model, by day", "width": 6}],
        [{"chart": "Cost per landed task, by day", "width": 6}, {"chart": "Landed tasks per day", "width": 6}],
        [{"chart": "First-try build rate, by day", "width": 6}, {"chart": "Lead time, launch to land (median hours), by day", "width": 6}],
        [{"chart": "Spend by task outcome, last 14 days", "width": 12}],
        [{"chart": "Reviewer agreement", "width": 4}, {"chart": "Arbiter decisions", "width": 4}, {"chart": "Build attempts per task", "width": 4}],
        [{"chart": "Cost per day by model alias", "width": 6}, {"chart": "Cost by role, last 7 days", "width": 6}],
        [{"chart": "Busy lanes per hour", "width": 12}],
        [{"chart": "Runs per day", "width": 12}],
        [{"chart": "Quarantined attempts per day", "width": 6}, {"chart": "Quarantines by cause, last 14 days", "width": 6}],
        [{"chart": "Tool uses by name, top 15", "width": 12}],
    ]
    assert _dashboard("Coxswain") == SPECS["dashboard"]


def test_every_dataset_sql_reads_only_under_data_runs():
    for d in SPECS["datasets"]:
        sql = bootstrap.expand_store(d["sql"], None)
        assert isinstance(sql, str), d["name"]
        sql = bootstrap.expand_lake(sql)
        assert isinstance(sql, str), d["name"]
        paths = LITERAL_READS.findall(sql)
        assert len(READS.findall(sql)) == len(paths), f"{d['name']} reads a source that is not a literal path"
        assert all(p.startswith("/data/runs/") for p in paths), d["name"]


def test_names_are_unique_within_each_kind():
    cells = [c["chart"] for row in SPECS["dashboard"]["grid"] for c in row]
    for names in ([d["name"] for d in SPECS["datasets"]], [c["name"] for c in SPECS["charts"]], cells):
        assert not [n for n, k in Counter(names).items() if k > 1]
    # Every chart lives on exactly one of the three dashboards; none is orphaned or shared.
    all_cells = [c["chart"] for dash in SPECS["dashboards"] for row in dash["grid"] for c in row]
    assert sorted(all_cells) == sorted(c["name"] for c in SPECS["charts"])


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
    assert all(sum(c["width"] for c in row) == 12 for dash in SPECS["dashboards"] for row in dash["grid"])


def test_plan_on_an_empty_server_creates_everything_in_order():
    ops = bootstrap.plan(SPECS, {}, ALL_SOURCES)
    assert {o.action for o in ops} == {"create"}
    kinds = [o.kind for o in ops]
    assert kinds == sorted(kinds, key=bootstrap.KINDS.index)
    assert Counter(kinds) == {"database": 1, "dataset": 18, "chart": 25, "dashboard": 3}


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


def test_the_ten_history_datasets_require_lake_and_read_no_local_runs_files():
    by_name = {d["name"]: d for d in SPECS["datasets"]}
    assert sorted(LAKE_DATASETS) == sorted(n for n, d in by_name.items() if d["requires"] == "lake")
    for name in LAKE_DATASETS:
        sql = by_name[name]["sql"]
        assert "read_parquet" not in sql
        assert "sqlite_scan" not in sql


def test_every_datasets_requires_is_lake_or_store():
    assert {d["requires"] for d in SPECS["datasets"]} == {"lake", "store"}


def test_store_without_lake_skips_the_ten_history_datasets_and_keeps_chair_fleet_and_the_database():
    ops = bootstrap.plan(SPECS, {}, frozenset({"store"}))
    skipped_datasets = {o.name for o in ops if o.kind == "dataset" and o.action == "skipped"}
    assert skipped_datasets == set(LAKE_DATASETS)
    assert {o.name for o in ops if o.kind == "dataset" and o.action == "create"} == {d["name"] for d in SPECS["datasets"] if d["requires"] == "store"}
    skipped_charts = {o.name for o in ops if o.kind == "chart" and o.action == "skipped"}
    assert skipped_charts == {c["name"] for c in SPECS["charts"] if c["dataset"] in LAKE_DATASETS}
    assert {o.name for o in ops if o.kind == "chart" and o.action == "create"} == {c["name"] for c in SPECS["charts"] if c["dataset"] not in LAKE_DATASETS}
    (database_op,) = [o for o in ops if o.kind == "database"]
    assert database_op.action == "create"
    assert {o.name for o in ops if o.kind == "dashboard" and o.action == "skipped"} == {"Coxswain"}
    assert {o.name for o in ops if o.kind == "dashboard" and o.action != "skipped"} == {"Fleet", "Chair"}


def test_present_sources_reports_store_and_lake_from_the_two_probes():
    assert bootstrap.present_sources() == frozenset()
    assert bootstrap.present_sources(store=True) == frozenset({"store"})
    assert bootstrap.present_sources(lake=True) == frozenset({"lake"})
    assert bootstrap.present_sources(True, True) == ALL_SOURCES


def test_plan_without_store_skips_every_chair_and_fleet_dataset_and_both_dashboards():
    ops = bootstrap.plan(SPECS, {}, ALL_SOURCES - {"store"})
    assert [bootstrap.line(o) for o in ops if o.action == "skipped"] == (
        [f"skipped: {n} (no store tables yet)" for n in STORE_GATED_DATASETS]
        + [f"skipped: {chart} (dataset {dataset} skipped)" for chart, dataset in STORE_GATED_CHARTS]
        + ["skipped: Fleet (no charts)", "skipped: Chair (no charts)"]
    )
    assert [o.name for o in ops if o.kind == "dataset" and o.action == "create"] == [d["name"] for d in SPECS["datasets"] if d["name"] not in STORE_GATED_DATASETS]
    assert sorted(c["name"] for c in SPECS["charts"] if c["dataset"] in STORE_GATED_DATASETS) == sorted(chart for chart, _ in STORE_GATED_CHARTS)
    # Every Fleet and every Chair chart is store-gated, so both dashboards' grids empty out and skip too.
    assert Counter(o.kind for o in ops if o.action == "create") == {"database": 1, "dataset": 10, "chart": 17, "dashboard": 1}


def test_no_chair_dataset_sql_names_a_log_file_or_a_scan():
    sqls = {d["name"]: d["sql"] for d in SPECS["datasets"] if d["name"] in CHAIR_DATASETS}
    postgres = {n: bootstrap.expand_store(sql, "postgresql://u@h/db") for n, sql in sqls.items()}
    assert sorted(sqls) == sorted(CHAIR_DATASETS)
    assert not [n for n, sql in sqls.items() if ".jsonl" in sql or "sqlite_scan" in sql or READS.findall(sql)]
    assert not [n for n, sql in postgres.items() if ".jsonl" in sql or "sqlite_scan" in sql or "store.public." not in sql]
    assert all(d["requires"] == "store" for d in SPECS["datasets"] if d["name"] in CHAIR_DATASETS)


class _SqlLab:
    """SQL Lab over a store holding `tables`. A select on any other table answers `code` with a query-error body."""

    def __init__(self, tables, code=400):
        self.tables, self.code, self.sent = tables, code, []

    def call(self, method, path, data=None):
        self.sent.append(data["sql"])
        if re.search(r"'(\w+)'\) WHERE false$", data["sql"])[1] not in self.tables:
            body = io.BytesIO(json.dumps({"errors": [{"message": "error"}]}).encode())
            raise urllib.error.HTTPError(path, self.code, "error", {}, body)
        return {"data": []}


def test_the_chair_probe_selects_where_false_from_every_table_it_reads_and_needs_all():
    full = _SqlLab({"chair_actions", "leases", "runs"})
    assert bootstrap.chair_tables_readable(full, 1, None) is True
    assert full.sent[0] == "SELECT ts, kind, target, status, reason, action_json FROM sqlite_scan('/data/runs/cox.db', 'chair_actions') WHERE false"
    assert full.sent[1] == "SELECT name, holder, epoch, heartbeat_at FROM sqlite_scan('/data/runs/cox.db', 'leases') WHERE false"
    assert full.sent[2] == "SELECT host, launched_at, ended_at FROM sqlite_scan('/data/runs/cox.db', 'runs') WHERE false"
    assert bootstrap.chair_tables_readable(_SqlLab({"chair_actions", "leases"}), 1, None) is False
    assert not any("chair_ticks" in probe for probe in bootstrap.CHAIR_PROBES)


def test_the_chair_probe_raises_on_a_401_even_though_the_body_carries_errors():
    with pytest.raises(urllib.error.HTTPError) as raised:
        bootstrap.chair_tables_readable(_SqlLab(set(), code=401), 1, None)
    assert raised.value.code == 401


def test_the_hosts_probe_selects_where_false_from_the_hosts_table():
    full = _SqlLab({"hosts"})
    assert bootstrap.hosts_table_readable(full, 1, None) is True
    assert full.sent == ["SELECT name, state, capacity, beat_at, versions_json FROM sqlite_scan('/data/runs/cox.db', 'hosts') WHERE false"]
    assert bootstrap.hosts_table_readable(_SqlLab({"chair_actions"}), 1, None) is False


class _LakeLab:
    """SQL Lab where the lake.runs probe succeeds only when `readable` is True."""

    def __init__(self, readable):
        self.readable, self.sent = readable, []

    def call(self, method, path, data=None):
        self.sent.append(data["sql"])
        if not self.readable:
            body = io.BytesIO(json.dumps({"errors": [{"message": "error"}]}).encode())
            raise urllib.error.HTTPError(path, 400, "error", {}, body)
        return {"data": []}


def test_the_lake_probe_selects_where_false_from_lake_runs():
    lab = _LakeLab(True)
    assert bootstrap.lake_readable(lab, 1, None) is True
    assert lab.sent == [f"SELECT * FROM {bootstrap.LAKE_SCHEMA}.runs WHERE false"]
    assert bootstrap.lake_readable(_LakeLab(False), 1, None) is False


CHAIR_STORE = """
SET TimeZone = 'UTC';
CREATE TABLE node_calls AS SELECT * FROM (VALUES ('2026-09-20T10:00:00Z', 3.0)) AS t(ts, cost_usd);
CREATE TABLE chair_actions AS SELECT * FROM (VALUES
  ('2026-09-21T09:00:00Z', 3, 'me@box1:1', 'needs_chair', 'i/a', 'ok', '', '{"initiative":"i","run":"r1","phase":"p1","reason":"review"}'),
  ('2026-09-21T09:00:00Z', 3, 'me@box1:1', 'needs_chair', 'i/b', 'ok', '', '{"initiative":"i","run":"r1","phase":"p1","reason":"conflict"}'),
  ('2026-09-21T10:00:00Z', 3, 'me@box1:1', 'land', 'i/a', 'landed', '', '{}'),
  ('2026-09-21T10:00:00Z', 3, 'me@box1:1', 'land', 'i/b', 'failed', 'merge', '{}')
) AS t(ts, epoch, holder, kind, target, status, reason, action_json);
CREATE TABLE leases AS SELECT * FROM (VALUES
  ('chair', 'me@box1:12345', 3, '2026-09-21T11:00:00Z', '2026-09-21T11:05:00Z')
) AS t(name, holder, epoch, heartbeat_at, expires_at);
CREATE TABLE runs AS SELECT '' AS host, CAST(now() - INTERVAL 2 HOUR AS VARCHAR) AS launched_at, CAST(NULL AS VARCHAR) AS ended_at;
CREATE TABLE hosts AS SELECT * FROM (VALUES
  ('local', 'ssh://local', 8, 'active', '2026-09-21T09:00:00Z', '{}', '2026-09-21T09:00:00Z', 'me')
) AS t(name, ssh, capacity, state, beat_at, versions_json, updated_at, updated_by);
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
        (datetime.date(2026, 9, 20), 3.0, 0, None),
    ]
    assert [(r[0], r[4]) for r in _chair_rows("chair_needs_backlog")] == [("i/b", "conflict")]
    (status,) = _chair_rows("chair_status")
    assert status[:3] == ("me@box1:12345", "box1", 3) and status[3] > 0
    assert len(status) == 4
    assert {(r[1], r[3]) for r in _chair_rows("chair_lanes_by_host_hour")} == {("local", 8)}
    assert sorted((r[1], r[2]) for r in _chair_rows("chair_actions_by_hour")) == [("land", 2), ("needs_chair", 2)]


def test_the_chair_backlog_flagged_at_is_a_naive_utc_datetime():
    (row,) = [r for r in _chair_rows("chair_needs_backlog") if r[0] == "i/b"]
    assert row[5] == datetime.datetime.fromisoformat("2026-09-21T09:00:00")
    assert row[5].tzinfo is None


FLEET_STORE = """
SET TimeZone = 'UTC';
CREATE TABLE hosts AS SELECT * FROM (VALUES
  ('box1', 'ssh://box1', 8, 'active', '2026-09-21T09:00:00Z', '{"login_ok": true, "login_checked_at": "2026-09-21T09:00:00Z"}', '2026-09-21T09:00:00Z', 'me'),
  ('box2', 'ssh://box2', 4, 'quarantined', '2026-09-19T08:00:00Z', '{"login_ok": false, "login_checked_at": "2026-09-19T08:00:00Z"}', '2026-09-19T08:00:00Z', 'me')
) AS t(name, ssh, capacity, state, beat_at, versions_json, updated_at, updated_by);
CREATE TABLE chair_actions AS SELECT * FROM (VALUES
  ('2026-09-21T09:00:00Z', 3, 'me@box1:1', 'needs_chair', 'i/a', 'ok', '', '{"reason":"review"}'),
  ('2026-09-21T09:00:00Z', 3, 'me@box1:1', 'needs_chair', 'i/b', 'ok', '', '{"reason":"conflict"}'),
  ('2026-09-21T10:00:00Z', 3, 'me@box1:1', 'land', 'i/a', 'landed', '', '{}'),
  ('2026-09-21T10:00:00Z', 3, 'me@box1:1', 'land', 'i/b', 'failed', 'merge', '{}')
) AS t(ts, epoch, holder, kind, target, status, reason, action_json);
CREATE TABLE node_calls AS SELECT * FROM (VALUES
  ('2026-09-14T11:00:00Z', 2.0),
  ('2026-09-15T11:00:00Z', 3.0),
  ('2026-09-21T11:00:00Z', 1.0)
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
        ("box1", "active", 8, datetime.datetime.fromisoformat("2026-09-21T09:00:00"), True, datetime.datetime.fromisoformat("2026-09-21T09:00:00")),
        ("box2", "quarantined", 4, datetime.datetime.fromisoformat("2026-09-19T08:00:00"), False, datetime.datetime.fromisoformat("2026-09-19T08:00:00")),
    ]
    assert all(r[3].tzinfo is None for r in _fleet_rows("fleet_host_state"))
    assert [(r[0], r[1]) for r in _fleet_rows("fleet_needs_chair_by_cause")] == [("conflict", 1)]
    weekly = _fleet_rows("fleet_weekly_spend")
    assert [round(r[1], 2) for r in weekly] == [5.0, 1.0]
    assert weekly[0][0] == datetime.datetime.fromisoformat("2026-09-14T00:00:00")
    assert weekly[1][0] == datetime.datetime.fromisoformat("2026-09-21T00:00:00")
    assert not [d for d in SPECS["datasets"] if d["name"] == "fleet_drafts_waiting_approval"]


def test_no_fleet_dataset_sql_names_a_log_file_or_a_scan():
    sqls = {d["name"]: d["sql"] for d in SPECS["datasets"] if d["name"] in FLEET_DATASETS}
    postgres = {n: bootstrap.expand_store(sql, "postgresql://u@h/db") for n, sql in sqls.items()}
    assert sorted(sqls) == sorted(FLEET_DATASETS)
    assert not [n for n, sql in sqls.items() if ".jsonl" in sql or "sqlite_scan" in sql or READS.findall(sql)]
    assert not [n for n, sql in postgres.items() if ".jsonl" in sql or "sqlite_scan" in sql or "store.public." not in sql]


def test_no_dataset_sql_names_chair_ticks():
    assert not [d["name"] for d in SPECS["datasets"] if "chair_ticks" in d["sql"]]


def test_every_chair_and_hosts_dataset_runs_against_the_stores_real_columns():
    """A dataset naming a column the store doesn't have fails to execute against this fixture."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS store")
    con.execute("CREATE SCHEMA store.public")
    con.execute(
        "CREATE TABLE store.public.chair_actions "
        "(ts VARCHAR, epoch INTEGER, holder VARCHAR, kind VARCHAR, target VARCHAR, status VARCHAR, reason VARCHAR, action_json VARCHAR)"
    )
    con.execute(
        "CREATE TABLE store.public.hosts "
        "(name VARCHAR, ssh VARCHAR, capacity INTEGER, state VARCHAR, beat_at VARCHAR, versions_json VARCHAR, updated_at VARCHAR, updated_by VARCHAR)"
    )
    con.execute("CREATE TABLE store.public.leases (name VARCHAR, holder VARCHAR, epoch INTEGER, heartbeat_at VARCHAR, expires_at VARCHAR)")
    con.execute("CREATE TABLE store.public.runs (host VARCHAR, launched_at VARCHAR, ended_at VARCHAR)")
    # node_calls is not one of the tables the ticket's schema names, but chair_spend_by_day and
    # fleet_weekly_spend both read it; its shape is unchanged by this task.
    con.execute("CREATE TABLE store.public.node_calls (ts VARCHAR, cost_usd DOUBLE)")
    gated = {d["name"]: d["sql"] for d in SPECS["datasets"] if d["requires"] == "store"}
    assert sorted(gated) == sorted(STORE_GATED_DATASETS)
    for sql in gated.values():
        con.execute(bootstrap.expand_store(sql, "postgresql://u@h/db"))
    # The probes gate whether those datasets are created, so each must run against the real columns too.
    for probe in bootstrap.CHAIR_PROBES + bootstrap.HOSTS_PROBES:
        con.execute(bootstrap.expand_store(probe, "postgresql://u@h/db"))


def test_a_later_run_adds_the_lake_datasets_and_the_coxswain_dashboard_once_lake_appears():
    first = bootstrap.plan(SPECS, {}, ALL_SOURCES - {"lake"})
    ops = bootstrap.plan(SPECS, _server(first), ALL_SOURCES)
    created = {(o.kind, o.name) for o in ops if o.action == "create"}
    assert created == {
        *(("dataset", n) for n in LAKE_DATASETS),
        *(("chart", c["name"]) for c in SPECS["charts"] if c["dataset"] in LAKE_DATASETS),
        ("dashboard", "Coxswain"),
    }
    assert {o.action for o in ops} == {"create", "unchanged"}


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
    "Lanes per host over time against capacity": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "hour", "sqlExpression": "hour", "timeGrain": "PT1H"}, "host"],
            "metrics": [
                {"expressionType": "SIMPLE", "column": {"column_name": "lanes"}, "aggregate": "MAX", "label": "lanes"},
                {"expressionType": "SIMPLE", "column": {"column_name": "max_in_flight"}, "aggregate": "MAX", "label": "cap"},
            ],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "PT1H"},
        }
    ],
    # Table charts pass all_columns, a field queries_of has no branch for, so their saved
    # "columns" and "metrics" stay empty here; only x_axis_sort (Needs-chair backlog) shows up.
    "Host state and last login": [{"columns": [], "metrics": [], "orderby": [], "row_limit": 10000, "filters": [], "extras": {}}],
    "Chair holder, epoch and beat age": [{"columns": [], "metrics": [], "orderby": [], "row_limit": 10000, "filters": [], "extras": {}}],
    "Needs-chair items by cause": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "cause", "sqlExpression": "cause"}],
            "metrics": [{"expressionType": "SIMPLE", "column": {"column_name": "items"}, "aggregate": "SUM", "label": "items"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {},
        }
    ],
    "Weekly spend against the ceiling": [
        {
            "columns": [DAY_AXIS],
            "metrics": [
                {"expressionType": "SIMPLE", "column": {"column_name": "cumulative_cost_usd"}, "aggregate": "MAX", "label": "spend"},
                {"expressionType": "SIMPLE", "column": {"column_name": "weekly_fraction"}, "aggregate": "MAX", "label": "fraction of ceiling"},
            ],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Chair actions per hour by kind": [
        {
            "columns": [{"columnType": "BASE_AXIS", "expressionType": "SQL", "label": "hour", "sqlExpression": "hour", "timeGrain": "PT1H"}, "kind"],
            "metrics": [{"expressionType": "SIMPLE", "column": {"column_name": "actions"}, "aggregate": "SUM", "label": "actions"}],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "PT1H"},
        }
    ],
    "Spend per landed task per day": [
        {
            "columns": [DAY_AXIS],
            "metrics": [
                {"expressionType": "SIMPLE", "column": {"column_name": "cost_per_landed"}, "aggregate": "MAX", "label": "interactive chair"},
                {"expressionType": "SIMPLE", "column": {"column_name": "loop_cost_per_landed"}, "aggregate": "MAX", "label": "chair loop"},
            ],
            "orderby": [],
            "row_limit": 10000,
            "filters": [],
            "extras": {"time_grain_sqla": "P1D"},
        }
    ],
    "Needs-chair backlog": [{"columns": [], "metrics": [], "orderby": [["waiting_hours", False]], "row_limit": 10000, "filters": [], "extras": {}}],
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
        d["name"]: {
            "id": dataset_ids[d["name"]],
            "table_name": d["name"],
            "database": {"id": 1, "database_name": "coxswain", "backend": "duckdb"},
            "sql": bootstrap.expand_lake(bootstrap.expand_store(d["sql"], None)),
        }
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
    dashboards = {
        d["name"]: {"id": i, "dashboard_title": d["name"], "position_json": json.dumps(bootstrap.position(d["grid"], chart_ids, d["name"]))}
        for i, d in enumerate(SPECS["dashboards"], start=1)
    }
    return {"database": {"coxswain": database}, "dataset": datasets, "chart": charts, "dashboard": dashboards}, {i: n for n, i in dataset_ids.items()}


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
