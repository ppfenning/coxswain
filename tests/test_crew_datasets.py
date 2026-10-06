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

REVIEW_DATASETS = ("crew_tasks", "crew_reviews", "crew_task_roles")
REVIEW = {d["name"]: d for d in SPECS["datasets"] if d["name"] in REVIEW_DATASETS}

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


# Tasks: r1/a is a first-try approval, r1/b a two-attempt approval whose earliest build used haiku
# and whose arbiter sided with the charter reviewer. r2/c was rejected by the charter reviewer. r2/d
# lost an arbitration that sided with the adversary. r2/a reuses task_id a in another run, and its
# arbiter sided with neither. r2/e has an arbitration with no sided_with and no calls.
REVIEW_STORE = """
CREATE TABLE node_calls AS SELECT * FROM (VALUES
  ('2026-09-20T10:00:00Z', 'r1', 'a', 'build', 'sonnet', 1.0),
  ('2026-09-20T10:30:00Z', 'r1', 'a', 'review', 'opus', 0.5),
  ('2026-09-20T11:00:00Z', 'r1', 'b', 'build', 'sonnet', 1.0),
  ('2026-09-20T09:00:00Z', 'r1', 'b', 'build', 'haiku', 0.25),
  ('2026-09-20T12:00:00Z', 'r1', 'b', 'review', 'opus', 0.5),
  ('2026-09-20T13:00:00Z', 'r2', 'c', 'build', 'sonnet', 2.0),
  ('2026-09-20T14:00:00Z', 'r2', 'd', 'build', 'opus', 0.75),
  ('2026-09-20T15:00:00Z', 'r2', 'a', 'build', 'haiku', 0.5)
) AS t(ts, run_id, task_id, role, model_alias, cost_usd);
CREATE TABLE runs AS SELECT * FROM (VALUES ('r1', 'coxswain'), ('r2', 'graphs')) AS t(run_id, repo);
CREATE TABLE task_records AS SELECT * FROM (VALUES
  ('r1', 'a', '2026-09-20T10:45:00Z',
   '{"landed": true, "review": {"verdict": "approve"}, "adversary": {"verdict": "approve"}}'),
  ('r1', 'b', '2026-09-20T12:30:00Z',
   '{"review": {"verdict": "approve"}, "adversary": {"verdict": "revise"},
     "arbitration": {"verdict": "approve", "sided_with": "charter"}}'),
  ('r2', 'c', '2026-09-20T13:30:00Z', '{"review": {"verdict": "revise"}}'),
  ('r2', 'd', '2026-09-20T14:30:00Z',
   '{"review": {"verdict": "approve"}, "adversary": {"verdict": "revise"},
     "arbitration": {"verdict": "revise", "sided_with": "adversary"}}'),
  ('r2', 'a', '2026-09-20T15:30:00Z',
   '{"review": {"verdict": "approve"}, "adversary": {"verdict": "revise"},
     "arbitration": {"verdict": "revise", "sided_with": "neither"}}'),
  ('r2', 'e', '2026-09-20T16:30:00Z',
   '{"review": {"verdict": "approve"}, "adversary": {"verdict": "revise"}, "arbitration": {"verdict": "revise"}}')
) AS t(run_id, task_id, updated_at, record_json);
"""


def _rows(name, store=CREW_STORE):
    """The dataset's column names and rows over a literal store, each placeholder read as a plain table."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(store)
    cur = con.execute(bootstrap.STORE_FORM.sub(lambda m: m[1], {**CREW, **REVIEW}[name]["sql"]))
    return [c[0] for c in cur.description], cur.fetchall()


def _at(text):
    return datetime.datetime.fromisoformat(text)


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


def test_crew_tasks_flags_first_try_two_attempt_and_unapproved_tasks_over_literal_rows():
    columns, rows = _rows("crew_tasks", REVIEW_STORE)
    assert columns == ["run_id", "task_id", "repo", "builder_seat", "build_attempts", "first_try", "approved", "cost_usd", "at"]
    assert sorted(rows, key=lambda r: (r[0], r[1])) == [
        ("r1", "a", "coxswain", "sonnet", 1, True, True, 1.5, _at("2026-09-20T10:45:00")),
        ("r1", "b", "coxswain", "haiku", 2, False, True, 1.75, _at("2026-09-20T12:30:00")),
        ("r2", "a", "graphs", "haiku", 1, False, False, 0.5, _at("2026-09-20T15:30:00")),
        ("r2", "c", "graphs", "sonnet", 1, False, False, 2.0, _at("2026-09-20T13:30:00")),
        ("r2", "d", "graphs", "opus", 1, False, False, 0.75, _at("2026-09-20T14:30:00")),
        ("r2", "e", "graphs", None, 0, False, False, None, _at("2026-09-20T16:30:00")),
    ]
    assert all(r[8].tzinfo is None for r in rows)


def test_crew_reviews_reads_verdicts_defaults_and_the_arbiters_side_over_literal_rows():
    columns, rows = _rows("crew_reviews", REVIEW_STORE)
    assert columns == ["run_id", "task_id", "charter_verdict", "adversary_verdict", "arbiter_verdict", "arbiter_side", "at"]
    assert sorted(rows, key=lambda r: (r[0], r[1])) == [
        ("r1", "a", "approve", "approve", "not needed", "not needed", _at("2026-09-20T10:45:00")),
        ("r1", "b", "approve", "revise", "approve", "charter", _at("2026-09-20T12:30:00")),
        ("r2", "a", "approve", "revise", "revise", "neither", _at("2026-09-20T15:30:00")),
        ("r2", "c", "revise", "none", "not needed", "not needed", _at("2026-09-20T13:30:00")),
        ("r2", "d", "approve", "revise", "revise", "adversary", _at("2026-09-20T14:30:00")),
        ("r2", "e", "approve", "revise", "revise", "not recorded", _at("2026-09-20T16:30:00")),
    ]
    assert all(r[6].tzinfo is None for r in rows)


def test_crew_task_roles_has_one_row_per_task_and_role_with_cost_and_approval_over_literal_rows():
    columns, rows = _rows("crew_task_roles", REVIEW_STORE)
    assert columns == ["run_id", "task_id", "role", "approved", "cost_usd", "calls", "at"]
    assert sorted(rows, key=lambda r: (r[0], r[1], r[2])) == [
        ("r1", "a", "build", True, 1.0, 1, _at("2026-09-20T10:00:00")),
        ("r1", "a", "review", True, 0.5, 1, _at("2026-09-20T10:30:00")),
        ("r1", "b", "build", True, 1.25, 2, _at("2026-09-20T11:00:00")),
        ("r1", "b", "review", True, 0.5, 1, _at("2026-09-20T12:00:00")),
        ("r2", "a", "build", False, 0.5, 1, _at("2026-09-20T15:00:00")),
        ("r2", "c", "build", False, 2.0, 1, _at("2026-09-20T13:00:00")),
        ("r2", "d", "build", False, 0.75, 1, _at("2026-09-20T14:00:00")),
    ]
    assert all(r[6].tzinfo is None for r in rows)


def test_no_review_dataset_sql_names_a_log_file_a_scan_or_a_data_path():
    assert sorted(REVIEW) == sorted(REVIEW_DATASETS)
    assert not [n for n, d in REVIEW.items() if any(s in d["sql"] for s in (".jsonl", "sqlite_scan", "/data"))]
    assert {(d["database"], d["requires"]) for d in REVIEW.values()} == {("coxswain", "store")}
