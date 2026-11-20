import pytest

from harness import store_ddl_0020
from harness.store_ddl_0020 import ADDED, ADDITIVE, TABLES, statements
from harness.store_dialect import POSTGRES, SQLITE, connect
from harness.store_migrate import ADDITIVE_PREFIX, check_version, default_modules, migrate

NOW1 = "2026-11-19T00:00:00Z"
NOW2 = "2026-11-20T00:00:00Z"
LATEST = len(default_modules())
NEW_TABLES = ("phase_queue", "step_results", "review_rounds", "workers")


def test_the_migration_is_additive_and_creates_four_tables_with_their_keys():
    assert ADDITIVE is True
    assert tuple(n for n, _, _ in TABLES) == NEW_TABLES
    assert {n: k for n, _, k in TABLES} == {
        "phase_queue": ("initiative", "phase"),
        "step_results": ("run_id", "step"),
        "review_rounds": ("initiative", "phase", "round"),
        "workers": ("name",),
    }
    assert ADDED == (("hosts", "threads"), ("hosts", "capabilities_json"))


def test_the_two_dialects_differ_only_in_the_json_type():
    sqlite, postgres = statements(SQLITE), statements(POSTGRES)
    assert len(sqlite) == len(postgres) == 4 + 3 + 2
    assert [s.replace("JSONB", "TEXT") for s in postgres] == list(sqlite)
    assert "result_json JSONB NOT NULL" in postgres[1] and "result_json TEXT NOT NULL" in sqlite[1]
    assert postgres[-2:] == (
        "ALTER TABLE hosts ADD COLUMN threads INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE hosts ADD COLUMN capabilities_json JSONB",
    )


def test_every_new_table_column_is_not_null_and_each_create_and_index_repeats_safely():
    for sql in statements(SQLITE)[:4]:
        body = sql.split("(", 1)[1].rsplit("PRIMARY KEY", 1)[0]
        columns = [line.strip().rstrip(",") for line in body.strip().splitlines()]
        assert columns and all(c.endswith("NOT NULL") for c in columns), sql
    assert all("IF NOT EXISTS" in s for s in statements(SQLITE)[:7])


def test_a_store_at_nineteen_gains_the_tables_empty_and_a_host_keeps_its_row(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:19]) == 19
        c.execute(
            "INSERT INTO hosts (name, ssh, capacity, state, beat_at, versions_json, updated_at, updated_by) "
            "VALUES ('lane-host', 'lane-host', 4, 'active', '', '{}', '', 'chair')"
        )
        assert migrate(c, NOW2, default_modules()[:20]) == 20
        assert [c.query_all(f"SELECT COUNT(*) FROM {t}")[0][0] for t in NEW_TABLES] == [0, 0, 0, 0]
        assert c.query_all("SELECT name, threads, capabilities_json FROM hosts") == [("lane-host", 0, None)]
        recorded = c.query_one("SELECT description FROM schema_version WHERE version = 20")
        assert recorded == (ADDITIVE_PREFIX + store_ddl_0020.DESCRIPTION,)
        # a second migrate changes nothing: no statement re-runs, no second version row
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert c.query_one("SELECT COUNT(*) FROM schema_version WHERE version = 20") == (1,)
        assert check_version(c) == (LATEST, LATEST)
    finally:
        c.close()


def test_the_step_results_key_refuses_a_second_row_for_one_step(store_conn):
    insert = "INSERT INTO step_results ({}) VALUES ({})"
    row = {
        "run_id": "r1", "step": "task/t1/build", "initiative": "i", "phase": "p1", "task_id": "t1", "kind": "build",
        "round": 1, "ok": 1, "verdict": "", "subject_sha": "", "worker": "", "claim_epoch": 0, "started_at": NOW1,
        "finished_at": NOW1, "cost_usd": 0.0, "result_json": "{}", "artifacts_json": "[]",
    }
    marks = ", ".join(store_conn.dialect.placeholder for _ in row)
    store_conn.execute(insert.format(", ".join(row), marks), list(row.values()))
    with pytest.raises(Exception, match="(?i)unique|duplicate"):
        store_conn.execute(insert.format(", ".join(row), marks), list(row.values()))
