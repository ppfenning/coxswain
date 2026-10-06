import datetime
import importlib.util
import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"
_spec = importlib.util.spec_from_file_location("superset_bootstrap", DEPLOY / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)

SPECS = bootstrap.load_specs(DEPLOY / "dashboards")
CREW = {d["name"]: d for d in SPECS["datasets"] if d["name"] in ("crew_calls", "crew_quarantines")}

CREW_STORE = """
CREATE TABLE node_calls AS SELECT * FROM (VALUES
  ('2026-09-20T10:00:00Z', 'run1', 't1', 'build', 'sonnet', 1.5, 3, 100, 40),
  ('2026-09-20T11:00:00Z', 'run1', 't1', 'build', 'sonnet', 0.5, 1, 50, 10),
  ('2026-09-20T12:00:00Z', 'run1', 't1', 'review', 'opus', 2.0, 2, 80, 0),
  ('2026-09-21T09:00:00Z', 'run1', 't1', 'review', 'opus', 1.0, 1, 20, 5),
  ('2026-09-21T09:30:00Z', 'run2', 't9', 'plan', 'opus', 0.25, 1, 10, 0)
) AS t(ts, run_id, task_id, role, model_alias, cost_usd, turns, input_total, cache_read_tokens);
CREATE TABLE runs AS SELECT * FROM (VALUES
  ('run1', 'coxswain'),
  ('run2', 'graphs')
) AS t(run_id, repo);
CREATE TABLE attempts AS SELECT * FROM (VALUES
  ('2026-09-21T10:00:00Z', 'run1', 't1', 'refused', 'scope', 'touched a file outside surfaces'),
  ('2026-09-21T11:00:00Z', 'run1', 't1', 'infra', NULL, NULL),
  ('2026-09-21T12:00:00Z', 'run2', 't9', 'dropped', '', 'no builder'),
  ('2026-09-21T13:00:00Z', 'run2', 't9', 'landed', 'none', 'ok'),
  ('2026-09-21T14:00:00Z', 'run9', 't1', 'unverified', 'checks', 'no run row')
) AS t(ts, run_id, task_id, kind, cause, cause_why);
"""


def _rows(name):
    """The dataset's column names and rows over CREW_STORE, each placeholder read as a plain table."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(CREW_STORE)
    cur = con.execute(bootstrap.STORE_FORM.sub(lambda m: m[1], CREW[name]["sql"]))
    return [c[0] for c in cur.description], cur.fetchall()


def test_crew_calls_sums_by_day_role_and_model_over_literal_rows():
    columns, rows = _rows("crew_calls")
    assert columns == ["day", "role", "model_alias", "calls", "cost_usd", "turns", "input_total", "cache_read_tokens"]
    assert sorted(rows) == [
        (datetime.date(2026, 9, 20), "build", "sonnet", 2, 2.0, 4, 150, 50),
        (datetime.date(2026, 9, 20), "review", "opus", 1, 2.0, 2, 80, 0),
        (datetime.date(2026, 9, 21), "plan", "opus", 1, 0.25, 1, 10, 0),
        (datetime.date(2026, 9, 21), "review", "opus", 1, 1.0, 1, 20, 5),
    ]


def test_crew_quarantines_has_one_naive_row_per_quarantined_attempt_over_literal_rows():
    columns, rows = _rows("crew_quarantines")
    assert columns == ["at", "cause", "cause_why", "role", "repo"]
    assert sorted(rows, key=lambda r: r[0]) == [
        (datetime.datetime.fromisoformat("2026-09-21T10:00:00"), "scope", "touched a file outside surfaces", "review", "coxswain"),
        (datetime.datetime.fromisoformat("2026-09-21T11:00:00"), "unclassified", None, "review", "coxswain"),
        (datetime.datetime.fromisoformat("2026-09-21T12:00:00"), "unclassified", "no builder", "plan", "graphs"),
        (datetime.datetime.fromisoformat("2026-09-21T14:00:00"), "checks", "no run row", None, None),
    ]
    assert all(r[0].tzinfo is None for r in rows)


def test_neither_crew_dataset_sql_names_a_log_file_a_scan_or_a_data_path():
    assert sorted(CREW) == ["crew_calls", "crew_quarantines"]
    assert not [n for n, d in CREW.items() if any(s in d["sql"] for s in (".jsonl", "sqlite_scan", "/data"))]
    assert {(d["database"], d["requires"]) for d in CREW.values()} == {("coxswain", "store")}
    # Every FROM or JOIN reads a store placeholder or the dataset's own CTE, never a bare table or path.
    targets = {n: set(re.findall(r"\b(?:FROM|JOIN)\s+(\S+)", d["sql"])) for n, d in CREW.items()}
    assert targets == {
        "crew_calls": {"{{store:node_calls}}"},
        "crew_quarantines": {"{{store:node_calls}}", "{{store:attempts}}", "{{store:runs}}", "last_call"},
    }
