import re
from types import SimpleNamespace

import pytest

import harness.store_copy as store_copy
from harness import store_ddl_0021, store_ddl_0023, store_notify
from harness.store_ddl_0021 import (
    ADDITIVE,
    ADDITIVE_EXEMPT,
    BEATS,
    CHANNEL,
    TABLES,
    WATCHED,
    notified_columns,
    statements,
)
from harness.store_dialect import POSTGRES, SQLITE, connect
from harness.store_migrate import ADDITIVE_PREFIX, additive_violations, check_version, default_modules, migrate

NOW1 = "2026-11-20T00:00:00Z"
NOW2 = "2026-11-21T00:00:00Z"
LATEST = len(default_modules())


def test_the_migration_is_additive_only_with_its_two_stated_exemptions_on_both_dialects():
    assert ADDITIVE is True
    assert set(ADDITIVE_EXEMPT) == {"trigger", "function"}
    for dialect in (SQLITE, POSTGRES):
        assert additive_violations(statements(dialect), ADDITIVE_EXEMPT) == ()
    # Without the exemptions only Postgres's function and triggers are refused: sqlite gets neither.
    refused = additive_violations(statements(POSTGRES), {})
    assert len(refused) == 1 + len(WATCHED) and all("ADDITIVE_EXEMPT does not exempt" in r for r in refused)
    assert additive_violations(statements(SQLITE), {}) == ()


def test_sqlite_gets_the_queue_and_postgres_adds_the_function_its_revoke_and_one_trigger_per_watched_table():
    sqlite, postgres = statements(SQLITE), statements(POSTGRES)
    assert postgres[: len(sqlite)] == sqlite and len(sqlite) == 2
    assert not [s for s in sqlite if re.search(r"\b(TRIGGER|FUNCTION|REVOKE)\b", s)]
    assert postgres[3] == "REVOKE EXECUTE ON FUNCTION cox_notify() FROM PUBLIC"
    triggers = postgres[4:]
    ons = [re.search(r" ON (\w+) FOR EACH STATEMENT EXECUTE FUNCTION cox_notify\(\)$", s) for s in triggers]
    assert [m.group(1) for m in ons if m] == [t for t, _ in WATCHED]
    assert all(s.startswith("CREATE TRIGGER cox_notify AFTER INSERT OR DELETE OR TRUNCATE OR UPDATE") for s in triggers)


def test_the_function_only_notifies_with_the_invokers_rights_and_a_pinned_path():
    function = statements(POSTGRES)[2]
    assert "SECURITY DEFINER" not in function and "SET search_path = pg_catalog, pg_temp" in function
    assert f"PERFORM pg_catalog.pg_notify('{CHANNEL}', TG_TABLE_NAME);" in function
    assert function.endswith("$cox_notify$") and function.count("$cox_notify$") == 2
    assert "%" not in function  # psycopg would read it as a placeholder: migrate passes every statement a tuple
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|EXECUTE)\b", function.split("$cox_notify$")[1])


def test_the_watched_columns_are_the_tables_columns_at_this_version_and_only_a_heartbeat_is_left_out():
    known = {name: columns for name, columns, _ in store_copy.tables()}
    # A later migration that adds a watched table's column replaces its trigger with the longer list (0023: workers).
    now_watched = {**dict(WATCHED), **dict(store_ddl_0023.WATCHED)}
    assert now_watched == {t: known[t] for t, _ in WATCHED}
    assert all(set(beat) < set(dict(WATCHED)[t]) for t, beat in BEATS.items())
    assert "hosts" not in BEATS  # host_beat always sets versions_json, which is data
    assert notified_columns("workers", dict(WATCHED)["workers"]) == dict(WATCHED)["workers"][:-1]
    assert notified_columns("runs", dict(WATCHED)["runs"]) == dict(WATCHED)["runs"]
    by_table = {re.search(r" ON (\w+) FOR", s).group(1): s for s in statements(POSTGRES)[4:]}  # type: ignore[union-attr]
    for table, columns in WATCHED:
        expected = f"UPDATE OF {', '.join(notified_columns(table, columns))} ON" if table in BEATS else "UPDATE ON"
        assert expected in by_table[table], table


def test_the_queue_is_the_one_table_and_it_is_copied():
    assert [n for n, _, _ in TABLES] == ["chair_queue"]
    assert [n for n, _, _ in store_copy.plan(store_copy.tables())][-1] == "chair_queue"


def test_the_listener_keeps_only_its_channels_tables_once_each():
    note = lambda channel, payload: SimpleNamespace(channel=channel, payload=payload)  # noqa: E731
    found = [note(CHANNEL, "runs"), note(CHANNEL, "runs"), note("other", "hosts"), note(CHANNEL, "work_items")]
    assert store_notify.tables(found) == {"runs", "work_items"}
    assert store_notify.tables([]) == frozenset()


def test_a_store_at_twenty_gains_the_queue_and_a_repeat_changes_nothing(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:20]) == 20
        assert migrate(c, NOW2, default_modules()[:21]) == 21
        assert c.query_all("SELECT COUNT(*) FROM chair_queue") == [(0,)]
        recorded = c.query_one("SELECT description FROM schema_version WHERE version = 21")
        assert recorded == (ADDITIVE_PREFIX + store_ddl_0021.DESCRIPTION,)
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert c.query_one("SELECT COUNT(*) FROM schema_version WHERE version = 21") == (1,)
        assert check_version(c) == (LATEST, LATEST)
    finally:
        c.close()


# Serial: `cox_store` is one channel per server, so another xdist worker's writes (its own schema) are heard too.
@pytest.mark.serial
def test_a_committed_write_wakes_the_listener_once_and_a_rolled_back_one_never(store_url):
    """The one boundary test: what the trigger does on a real Postgres, heard through `store_notify.listen`. Two
    statements on one table in one transaction are one notification, delivered at commit; a rollback delivers none."""
    if not store_url.startswith("postgres"):
        assert store_notify.listen(store_url) is None  # sqlite has no NOTIFY: the caller keeps its plain sleep
        return
    writer = connect(store_url)
    migrate(writer, NOW1)
    listener = store_notify.listen(store_url)
    assert listener is not None
    insert = ("INSERT INTO landing_records (initiative, repo, state, record_json, created_at, updated_at) "
              "VALUES ('{0}', 'r', 'building', '{{}}', '', '')")
    try:
        with writer.transaction():
            writer.execute(insert.format("i1"))
            writer.execute(insert.format("i2"))
            assert listener.wait(0.5) == frozenset()  # nothing before the commit
        assert listener.wait(5) == {"landing_records"}
        assert listener.wait(0.5) == frozenset()  # and only once
        with pytest.raises(RuntimeError, match="roll back"), writer.transaction():
            writer.execute("DELETE FROM landing_records")
            raise RuntimeError("roll back")
        assert listener.wait(1) == frozenset()
        assert writer.query_one("SELECT COUNT(*) FROM landing_records") == (2,)
    finally:
        listener.close()
        writer.close()
