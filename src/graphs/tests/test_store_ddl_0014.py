from harness.store_ddl_0014 import statements
from harness.store_dialect import POSTGRES, SQLITE, connect
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-10-29T00:00:00Z"
NOW2 = "2026-10-30T00:00:00Z"
LATEST = len(default_modules())


def _shape(conn, table):
    """{column: (type, nullable, default)} for `table`, read from the dialect's own catalogue."""
    if conn.dialect is POSTGRES:
        sql = (
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns"
            f" WHERE table_schema = current_schema() AND table_name = '{table}'"
        )
        return {n: (t.upper(), nullable == "YES", d) for n, t, nullable, d in conn.query_all(sql)}
    return {r[1]: (r[2].upper(), r[3] == 0, r[4]) for r in conn.query_all(f"PRAGMA table_info({table})")}


def test_the_statement_is_one_plain_text_alter_on_both_dialects():
    expected = ("ALTER TABLE leases ADD COLUMN progress TEXT",)
    assert statements(SQLITE) == expected
    assert statements(POSTGRES) == expected


def test_a_database_at_version_thirteen_gains_progress_and_old_rows_read_null(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:13]) == 13
        c.execute(
            "INSERT INTO leases (name, holder, epoch, heartbeat_at, expires_at)"
            " VALUES ('l1', 'h1', 1, 'T0', 'T1')"
        )
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
        assert _shape(c, "leases")["progress"] == ("TEXT", True, None)
        assert c.query_all("SELECT name, progress FROM leases") == [("l1", None)]
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[13:14] == [(14, NOW2)]
    finally:
        c.close()
