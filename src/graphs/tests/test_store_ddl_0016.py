from harness.store_ddl_0016 import statements
from harness.store_dialect import POSTGRES, SQLITE, connect
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-11-12T00:00:00Z"
NOW2 = "2026-11-13T00:00:00Z"
LATEST = len(default_modules())


def test_the_statements_create_the_two_tables_the_same_on_both_dialects():
    sqlite, postgres = statements(SQLITE), statements(POSTGRES)
    assert sqlite == postgres
    assert [s.split("(")[0].strip() for s in sqlite] == [
        "CREATE TABLE IF NOT EXISTS landing_records",
        "CREATE TABLE IF NOT EXISTS land_queue",
    ]
    assert "PRIMARY KEY (initiative)" in sqlite[0] and "PRIMARY KEY (id)" in sqlite[1]


def test_a_database_at_version_fifteen_gains_both_tables_empty(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:15]) == 15
        assert migrate(c, NOW2, default_modules()[:16]) == 16
        assert c.query_all("SELECT initiative FROM landing_records") == []
        assert c.query_all("SELECT id FROM land_queue") == []
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[15:16] == [(16, NOW2)]
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
    finally:
        c.close()
