import importlib.util
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"
_spec = importlib.util.spec_from_file_location("superset_bootstrap", DEPLOY / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)

SQL = "select * from {{lake:runs}}"


def test_lake_mode_expands_the_placeholder_to_the_lake_schema():
    assert bootstrap.expand_lake(SQL) == "select * from lake.runs"


def test_a_malformed_placeholder_returns_a_store_error():
    assert isinstance(bootstrap.expand_lake("{{lake:runs;drop}}"), bootstrap.StoreError)
    assert isinstance(bootstrap.expand_lake("{{lake}}"), bootstrap.StoreError)


def test_sql_without_a_placeholder_passes_through_unchanged():
    assert bootstrap.expand_lake("select 1") == "select 1"


def test_lake_schema_is_the_literal_lake_catalog_name():
    assert bootstrap.LAKE_SCHEMA == "lake"
