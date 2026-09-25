import importlib.util
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"
_spec = importlib.util.spec_from_file_location("superset_bootstrap", DEPLOY / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)

SQL = "select * from {{store:runs}}"
SCAN = "select * from sqlite_scan('/data/runs/cox.db', 'runs')"


def _specs(sql):
    return {
        "database": {"name": "db", "sqlalchemy_uri": "duckdb:///:memory:"},
        "datasets": [{"name": "d", "database": "db", "requires": "sqlite", "sql": sql}],
        "charts": [],
        "dashboard": {"name": "board", "grid": []},
    }


def test_default_mode_scans_cox_db_with_no_url_and_with_a_non_postgres_url():
    assert bootstrap.expand_store(SQL, None) == SCAN
    assert bootstrap.expand_store(SQL, "mysql://u:p@h/db") == SCAN


def test_postgres_mode_reads_the_attached_catalog_and_keeps_the_url_out():
    out = bootstrap.expand_store(SQL, "postgresql://u:p@h/db")
    assert out == "select * from store.public.runs"
    assert "postgres_scan" not in out and "u:p" not in out


def test_malformed_placeholders_return_a_store_error():
    assert isinstance(bootstrap.expand_store("{{store:runs;drop}}", None), bootstrap.StoreError)
    assert isinstance(bootstrap.expand_store("{{store}}", None), bootstrap.StoreError)


def test_sql_without_a_placeholder_passes_through_unchanged():
    assert bootstrap.expand_store("select 1", "postgresql://h/db") == "select 1"


def test_plan_expands_dataset_sql_and_reports_a_bad_placeholder_as_an_error_op():
    ok = bootstrap.plan(_specs(SQL), {}, {"sqlite"})
    assert ok[1].payload["sql"] == SCAN
    bad = bootstrap.plan(_specs("{{store:a-b}}"), {}, {"sqlite"})[1]
    assert (bad.action, bad.name) == ("error", "d")
    assert bootstrap.line(bad) == f"error: d ({bad.reason})"


def test_store_catalog_is_a_plain_identifier():
    assert bootstrap.STORE_CATALOG.isidentifier()
