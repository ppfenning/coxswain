"""Migration 0023: `workers.machine_id`, additive, and a row an older build writes keeps working beside it."""
import pytest

from harness import store_copy, store_ddl_0023, store_notify
from harness.store_ddl_0023 import ADDED, ADDITIVE, statements
from harness.store_dialect import POSTGRES, SQLITE, connect
from harness.store_migrate import ADDITIVE_PREFIX, additive_violations, check_version, default_modules, migrate

NOW1 = "2026-11-21T00:00:00Z"
NOW2 = "2026-11-22T00:00:00Z"
LATEST = len(default_modules())

# The registry row as a build one migration older writes it: every column but machine_id.
OLD_COLUMNS = ("name", "capabilities_json", "lanes", "threads", "state", "version", "doctor_json", "doctor_at",
               "started_at", "beat_at")


def test_the_migration_is_one_column_with_a_default_and_additive_on_both_dialects():
    assert ADDITIVE is True and ADDED == (("workers", "machine_id"),)
    alter = "ALTER TABLE workers ADD COLUMN machine_id TEXT NOT NULL DEFAULT ''"
    assert statements(SQLITE) == (alter,)
    assert statements(POSTGRES)[0] == alter
    assert additive_violations(statements(SQLITE), {}) == ()
    assert additive_violations(statements(POSTGRES), store_ddl_0023.ADDITIVE_EXEMPT) == ()
    assert additive_violations(statements(POSTGRES), {}) != (), "the trigger is exempt on purpose, with its reason"


def test_on_postgres_the_workers_trigger_is_replaced_so_the_new_column_wakes_and_the_beat_still_does_not():
    (trigger,) = statements(POSTGRES)[1:]
    assert trigger.startswith("CREATE OR REPLACE TRIGGER cox_notify AFTER INSERT OR DELETE OR TRUNCATE OR UPDATE OF ")
    assert trigger.endswith(" ON workers FOR EACH STATEMENT EXECUTE FUNCTION cox_notify()")
    listed = trigger.split("UPDATE OF ")[1].split(" ON ")[0].split(", ")
    assert "machine_id" in listed and "beat_at" not in listed and "state" in listed


def test_a_store_copy_carries_the_machine():
    (workers,) = [t for t in store_copy.tables() if t[0] == "workers"]
    assert "machine_id" in workers[1]


def test_a_store_at_twenty_two_gains_the_column_and_an_older_build_s_insert_gets_the_default(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:22]) == 22
        assert migrate(c, NOW2, default_modules()[:23]) == 23
        recorded = c.query_one("SELECT description FROM schema_version WHERE version = 23")
        assert recorded == (ADDITIVE_PREFIX + store_ddl_0023.DESCRIPTION,)
        p = c.dialect.placeholder
        c.execute(f"INSERT INTO workers ({', '.join(OLD_COLUMNS)}) VALUES ({', '.join([p] * len(OLD_COLUMNS))})",
                  ("old", "[]", 1, 0, "up", "1.0", "{}", NOW1, NOW1, NOW1))
        assert c.query_one("SELECT machine_id FROM workers WHERE name = 'old'") == ("",)
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
    finally:
        c.close()


# Serial: `cox_store` is one channel per server, so another xdist worker's writes (its own schema) are heard too.
@pytest.mark.serial
def test_on_postgres_a_machine_change_wakes_the_listener_and_a_beat_does_not(store_url):
    """What the replaced trigger does on a real Postgres, heard through `store_notify.listen`."""
    if not store_url.startswith("postgres"):
        return  # sqlite has no NOTIFY (store_ddl_0021's tests)
    writer = connect(store_url)
    migrate(writer, NOW1)
    p = writer.dialect.placeholder
    writer.execute(f"INSERT INTO workers ({', '.join(OLD_COLUMNS)}) VALUES ({', '.join([p] * len(OLD_COLUMNS))})",
                   ("w", "[]", 1, 0, "up", "1.0", "{}", NOW1, NOW1, NOW1))
    listener = store_notify.listen(store_url)
    assert listener is not None
    try:
        writer.execute("UPDATE workers SET beat_at = 'later' WHERE name = 'w'")
        assert listener.wait(1) == frozenset(), "a beat wakes nobody"
        writer.execute("UPDATE workers SET machine_id = 'desk' WHERE name = 'w'")
        assert listener.wait(5) == {"workers"}
    finally:
        listener.close()
        writer.close()
