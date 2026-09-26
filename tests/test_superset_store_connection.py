import importlib.util
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"
SENTINEL = "SENTINEL_PW_9f3"
URL = f"postgresql://u:{SENTINEL}@h:5432/db"
ATTACH = f"ATTACH '{URL}' AS store (TYPE postgres, READ_ONLY)"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load("superset_bootstrap", DEPLOY / "bootstrap.py")


@pytest.fixture
def config(monkeypatch):
    for name in ("SUPERSET_SECRET_KEY", "SUPERSET_ADMIN_USERNAME", "SUPERSET_ADMIN_PASSWORD", "SUPERSET_ADMIN_EMAIL", "SUPERSET_ADMIN_FIRSTNAME", "SUPERSET_ADMIN_LASTNAME"):
        monkeypatch.setenv(name, "value")
    monkeypatch.delenv("COXSWAIN_STORE_URL", raising=False)
    return _load("superset_config", DEPLOY / "superset_config.py")


def _connection(module, log):
    return type("Conn", (), {"__module__": module, "execute": lambda self, sql: log.append(sql)})()


def _specs():
    return {
        "database": {"name": "db", "sqlalchemy_uri": "duckdb:///:memory:"},
        "datasets": [{"name": "d", "database": "db", "requires": "sqlite", "sql": "select * from {{store:runs}}"}],
        "charts": [],
        "dashboard": {"name": "board", "grid": []},
    }


def test_a_postgres_url_gives_the_exact_attach_statement(config):
    assert config.attach_sql(URL) == ATTACH
    assert config.attach_sql(URL.replace("postgresql", "postgres")) == ATTACH.replace("postgresql", "postgres")


def test_none_empty_and_a_sqlite_path_give_none(config):
    assert config.attach_sql(None) is None
    assert config.attach_sql("") is None
    assert config.attach_sql("/data/runs/cox.db") is None
    assert config.attach_sql("sqlite:////data/runs/cox.db") is None


def test_a_quote_in_the_url_is_doubled(config):
    assert config.attach_sql("postgresql://u:it's@h/db") == "ATTACH 'postgresql://u:it''s@h/db' AS store (TYPE postgres, READ_ONLY)"


def test_the_attach_catalog_is_the_bootstrap_catalog():
    assert bootstrap.STORE_CATALOG == "store"


def test_the_listener_loads_postgres_then_attaches_on_a_duckdb_connection(config, monkeypatch):
    monkeypatch.setenv("COXSWAIN_STORE_URL", URL)
    log = []
    config.attach_store(_connection("duckdb", log), None)
    assert log == ["LOAD postgres", ATTACH]


def test_the_listener_leaves_a_non_duckdb_connection_and_an_unset_url_alone(config, monkeypatch):
    log = []
    monkeypatch.setenv("COXSWAIN_STORE_URL", URL)
    config.attach_store(_connection("sqlite3", log), None)
    monkeypatch.setenv("COXSWAIN_STORE_URL", "")
    config.attach_store(_connection("duckdb", log), None)
    assert log == []


def test_the_sentinel_password_is_in_no_dataset_sql_and_no_saved_database_field():
    assert SENTINEL not in bootstrap.expand_store("select * from {{store:runs}}", URL)
    database, dataset = bootstrap.plan(_specs(), {}, {"sqlite"}, URL)[:2]
    saved = bootstrap.body(database, {})
    assert SENTINEL not in repr((database.payload, saved, dataset.payload))
