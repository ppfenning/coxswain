import re

import pytest

import harness.store_copy as store_copy
from harness import store_ddl_0021
from harness.store_ddl_0021 import ADDITIVE, ADDITIVE_EXEMPT, BEATS, GATE, TABLES, WATCHED, stamped_columns, statements
from harness.store_dialect import POSTGRES, SQLITE, connect
from harness.store_migrate import ADDITIVE_PREFIX, additive_violations, check_version, default_modules, migrate

NOW1 = "2026-11-20T00:00:00Z"
NOW2 = "2026-11-21T00:00:00Z"
LATEST = len(default_modules())
NAMES = (GATE, *(t for t, _ in WATCHED))


def test_the_migration_is_additive_only_with_its_two_stated_exemptions_on_both_dialects():
    assert ADDITIVE is True
    assert set(ADDITIVE_EXEMPT) == {"trigger", "function"}
    for dialect in (SQLITE, POSTGRES):
        assert additive_violations(statements(dialect), ADDITIVE_EXEMPT) == ()
    # Without the exemptions only Postgres's function and triggers are refused: sqlite gets neither.
    refused = additive_violations(statements(POSTGRES), {})
    assert len(refused) == 1 + len(WATCHED) and all("ADDITIVE_EXEMPT does not exempt" in r for r in refused)
    assert additive_violations(statements(SQLITE), {}) == ()


def test_sqlite_gets_the_tables_their_index_and_seed_rows_and_postgres_adds_the_function_and_triggers():
    sqlite, postgres = statements(SQLITE), statements(POSTGRES)
    assert postgres[: len(sqlite)] == sqlite and len(sqlite) == 4
    assert not [s for s in sqlite if re.search(r"\b(TRIGGER|FUNCTION)\b", s)]
    triggers = [re.search(r" ON (\w+) FOR EACH STATEMENT EXECUTE FUNCTION cox_stamp\(\)$", s) for s in postgres[5:]]
    assert [m.group(1) for m in triggers if m] == [t for t, _ in WATCHED]
    assert all(s.startswith("CREATE TRIGGER cox_stamp BEFORE INSERT OR DELETE OR UPDATE") for s in postgres[5:])
    assert re.findall(r"'([^']*)', 0, ''", sqlite[3]) == list(NAMES)
    assert sqlite[3].endswith("ON CONFLICT (name) DO NOTHING")


def test_the_function_runs_as_its_owner_with_a_pinned_path_and_takes_the_gate_before_its_table():
    function = statements(POSTGRES)[4]
    assert "SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $cox_stamp$" in function
    assert function.endswith("$cox_stamp$") and function.count("$cox_stamp$") == 2
    assert "%" not in function  # psycopg would read it as a placeholder: migrate passes every statement a tuple
    # The one statement it runs names store_cursor in the firing table's schema, and the gate goes first.
    assert "quote_ident(TG_TABLE_SCHEMA)" in function and "'.store_cursor SET seq = seq + 1" in function
    assert function.index("USING stamped, '*'") < function.index("USING stamped, TG_TABLE_NAME")
    assert "RETURN NULL;" in function


def test_the_watched_columns_are_the_tables_columns_at_this_version_and_only_a_heartbeat_is_left_out():
    known = {name: columns for name, columns, _ in store_copy.tables()}
    assert dict(WATCHED) == {t: known[t] for t, _ in WATCHED}
    assert all(set(beat) < set(dict(WATCHED)[t]) for t, beat in BEATS.items())
    assert stamped_columns("hosts", dict(WATCHED)["hosts"]) == (
        "name", "ssh", "capacity", "state", "updated_at", "updated_by", "threads", "capabilities_json")
    assert stamped_columns("runs", dict(WATCHED)["runs"]) == dict(WATCHED)["runs"]
    by_table = {re.search(r" ON (\w+) FOR", s).group(1): s for s in statements(POSTGRES)[5:]}  # type: ignore[union-attr]
    for table, columns in WATCHED:
        expected = f"UPDATE OF {', '.join(stamped_columns(table, columns))} ON" if table in BEATS else "UPDATE ON"
        assert expected in by_table[table], table


def test_the_queue_is_copied_and_the_cursor_is_not():
    names = [n for n, _, _ in store_copy.plan(store_copy.tables())]
    assert names[-1] == "chair_queue" and "store_cursor" not in names
    assert {n for n, _, _ in TABLES} == {"chair_queue"}


def test_a_store_at_twenty_gains_both_tables_with_the_cursor_seeded_at_zero_and_a_repeat_changes_nothing(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:20]) == 20
        assert migrate(c, NOW2, default_modules()[:21]) == 21
        assert c.query_all("SELECT COUNT(*) FROM chair_queue") == [(0,)]
        assert sorted(c.query_all("SELECT name, seq, at FROM store_cursor")) == sorted((n, 0, "") for n in NAMES)
        recorded = c.query_one("SELECT description FROM schema_version WHERE version = 21")
        assert recorded == (ADDITIVE_PREFIX + store_ddl_0021.DESCRIPTION,)
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert c.query_one("SELECT COUNT(*) FROM schema_version WHERE version = 21") == (1,)
        assert check_version(c) == (LATEST, LATEST)
    finally:
        c.close()


def _seqs(conn) -> dict[str, int]:
    return {str(n): int(s) for n, s in conn.query_all("SELECT name, seq FROM store_cursor")}  # type: ignore[arg-type]


def test_a_statement_bumps_the_gate_and_its_table_once_inside_the_writers_own_transaction(store_url):
    """The one boundary test: what the trigger does on a real Postgres. One statement writing two rows bumps its table
    and the gate once each; the bump is visible inside the writer's transaction and to no one else until the commit,
    and a rolled-back write leaves the cursor where it was."""
    if not store_url.startswith("postgres"):
        pytest.skip("sqlite has no trigger: the cursor is Postgres's")
    writer, reader = connect(store_url), connect(store_url)
    try:
        migrate(writer, NOW1)
        before = _seqs(reader)
        insert = ("INSERT INTO landing_records (initiative, repo, state, record_json, created_at, updated_at) "
                  "VALUES ('i1', 'r', 'building', '{}', '', ''), ('i2', 'r', 'building', '{}', '', '')")
        with writer.transaction():
            writer.execute(insert)
            inside = _seqs(writer)
            outside = _seqs(reader)
        after = _seqs(reader)
        assert {k: inside[k] - before[k] for k in before if inside[k] != before[k]} == {"*": 1, "landing_records": 1}
        assert outside == before
        assert after == inside
        assert reader.query_one("SELECT at FROM store_cursor WHERE name = 'landing_records'") != ("",)
        with pytest.raises(RuntimeError, match="roll back"), writer.transaction():
            writer.execute("DELETE FROM landing_records")
            raise RuntimeError("roll back")
        assert _seqs(reader) == after
    finally:
        writer.close()
        reader.close()
