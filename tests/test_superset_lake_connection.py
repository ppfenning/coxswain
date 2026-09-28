import importlib.util
import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"
SENTINEL = "SENTINEL_KEY_7c1"
ENV = {
    "COXSWAIN_S3_ENDPOINT": "http://garage:3900",
    "COXSWAIN_S3_KEY_ID": SENTINEL,
    "COXSWAIN_S3_SECRET": "shh",
    "COXSWAIN_S3_REGION": "eu-west",
}
SECRET = (
    "CREATE OR REPLACE SECRET lake_s3 (TYPE s3, "
    f"KEY_ID '{SENTINEL}', SECRET 'shh', REGION 'eu-west', "
    "ENDPOINT 'http://garage:3900', URL_STYLE 'path', USE_SSL false)"
)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(monkeypatch):
    for name in ("SUPERSET_SECRET_KEY", "SUPERSET_ADMIN_USERNAME", "SUPERSET_ADMIN_PASSWORD", "SUPERSET_ADMIN_EMAIL", "SUPERSET_ADMIN_FIRSTNAME", "SUPERSET_ADMIN_LASTNAME"):
        monkeypatch.setenv(name, "value")
    monkeypatch.delenv("COXSWAIN_STORE_URL", raising=False)
    for name in ("COXSWAIN_S3_ENDPOINT", "COXSWAIN_S3_KEY_ID", "COXSWAIN_S3_SECRET", "COXSWAIN_S3_REGION"):
        monkeypatch.delenv(name, raising=False)
    return _load("superset_config", DEPLOY / "superset_config.py")


STORE_URL = "postgresql://u:pw@h:5432/db"
ATTACH = f"ATTACH '{STORE_URL}' AS store (TYPE postgres, READ_ONLY)"


def _connection(module, log, rows=()):
    cursor = type("Cursor", (), {"fetchall": lambda self: list(rows)})()
    return type("Conn", (), {"__module__": module, "execute": lambda self, sql: (log.append(sql), cursor)[1]})()


def test_the_s3_secret_sql_from_a_literal_environment_mapping(config):
    assert config.lake_secret_sql(ENV) == SECRET


def test_the_region_defaults_to_garage_when_unset(config):
    env = {k: v for k, v in ENV.items() if k != "COXSWAIN_S3_REGION"}
    assert "REGION 'garage'" in config.lake_secret_sql(env)


def test_no_secret_sql_and_no_load_when_the_endpoint_is_unset(config, monkeypatch):
    assert config.lake_secret_sql({}) is None
    assert config.lake_secret_sql({"COXSWAIN_S3_ENDPOINT": ""}) is None
    monkeypatch.setenv("COXSWAIN_STORE_URL", STORE_URL)
    log = []
    config.attach_store(_connection("duckdb", log, [("runs", "s3://x.json")]), None)
    assert log == ["LOAD postgres", ATTACH]


def test_an_endpoint_adds_the_loads_the_secret_and_one_view_per_catalog_row(config, monkeypatch):
    monkeypatch.setenv("COXSWAIN_STORE_URL", STORE_URL)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    log = []
    config.attach_store(_connection("duckdb", log, [("runs", "s3://r.json"), ("phases", "s3://p.json")]), None)
    assert log == [
        "LOAD postgres",
        ATTACH,
        "LOAD httpfs",
        "LOAD iceberg",
        SECRET,
        config.LAKE_TABLES_SQL,
        "CREATE OR REPLACE VIEW lake.runs AS SELECT * FROM iceberg_scan('s3://r.json')",
        "CREATE OR REPLACE VIEW lake.phases AS SELECT * FROM iceberg_scan('s3://p.json')",
    ]


def test_the_view_sql_for_a_literal_table_name_and_metadata_location(config):
    assert config.lake_view_sql("runs", "s3://coxswain/lake/runs/metadata/00001.metadata.json") == (
        "CREATE OR REPLACE VIEW lake.runs AS SELECT * FROM iceberg_scan("
        "'s3://coxswain/lake/runs/metadata/00001.metadata.json')"
    )


def test_a_quote_in_a_credential_is_doubled_like_the_store_urls_password(config):
    env = {**ENV, "COXSWAIN_S3_SECRET": "it's"}
    assert "SECRET 'it''s'" in config.lake_secret_sql(env)


def test_a_quote_in_the_metadata_location_is_doubled(config):
    assert config.lake_view_sql("runs", "s3://it's/x.json") == "CREATE OR REPLACE VIEW lake.runs AS SELECT * FROM iceberg_scan('s3://it''s/x.json')"


def test_the_lake_schema_comment_names_the_same_schema_as_bootstraps_lake_schema():
    (value,) = re.findall(r'LAKE_SCHEMA = "(\w+)"', (DEPLOY / "bootstrap.py").read_text())
    assert value == "lake"
    assert f"`{value}`" in (DEPLOY / "superset_config.py").read_text()
