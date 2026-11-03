from harness.store_ddl_0015 import statements
from harness.store_dialect import POSTGRES, SQLITE, connect
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-11-02T00:00:00Z"
NOW2 = "2026-11-03T00:00:00Z"
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


def test_the_statements_are_two_plain_text_alters_on_both_dialects():
    expected = (
        "ALTER TABLE work_items ADD COLUMN merged_commit TEXT",
        "ALTER TABLE work_items ADD COLUMN handoff_json TEXT",
    )
    assert statements(SQLITE) == expected
    assert statements(POSTGRES) == expected


def test_a_database_at_version_fourteen_gains_both_columns_and_old_rows_read_null(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:14]) == 14
        c.execute(
            "INSERT INTO work_items (initiative, task_id, phase, state, needs_json, updated_at, updated_by)"
            " VALUES ('i1', 't1', 'p1', 'approved', '[]', 'T0', 'u')"
        )
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
        shape = _shape(c, "work_items")
        assert shape["merged_commit"] == ("TEXT", True, None)
        assert shape["handoff_json"] == ("TEXT", True, None)
        assert c.query_all("SELECT task_id, merged_commit, handoff_json FROM work_items") == [("t1", None, None)]
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[14:15] == [(15, NOW2)]
    finally:
        c.close()
