import sqlite3
import sys
from contextlib import contextmanager
from types import ModuleType

import pytest

import harness.store_migrate as store_migrate
from harness.store_dialect import POSTGRES, SQLITE, Connection, connect, forbidden_constructs
from harness.store_migrate import (
    ADDITIVE_PREFIX,
    MigrationError,
    accepts,
    additive_violations,
    check_version,
    default_modules,
    holds_tables,
    migrate,
    newer_descriptions,
    newer_refusal,
    open_store,
    pending,
    recorded_description,
    should_back_up,
)

NOW = "2026-09-24T00:00:00Z"


def fake(number, statements, name=None, additive=None):
    m = ModuleType(name or f"harness.store_ddl_{number:04d}")
    m.VERSION = number
    m.DESCRIPTION = f"fake {number}"
    m.statements = lambda dialect: tuple(statements)
    if additive is not None:
        m.ADDITIVE = additive
    return m


ONE = ("CREATE TABLE t1 (id INTEGER PRIMARY KEY)",)
TWO = ("CREATE TABLE t2 (id INTEGER PRIMARY KEY)", "INSERT INTO t2 (id) VALUES (1)")


@pytest.fixture
def conn(store_url):
    """An unmigrated store on either backend; open_store would migrate it first."""
    c = connect(store_url)
    yield c
    c.close()


def tables(c):
    if c.dialect is POSTGRES:
        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    else:
        sql = "SELECT name FROM sqlite_master WHERE type = 'table'"
    return {r[0] for r in c.query_all(sql)}


def missing_table_error(c):
    """The exception each driver raises for a statement naming a table that does not exist."""
    if c.dialect is POSTGRES:
        from psycopg.errors import UndefinedTable

        return UndefinedTable
    return sqlite3.OperationalError


def test_empty_database_applies_both_in_order_and_lands_at_two(conn):
    assert migrate(conn, NOW, [fake(2, TWO), fake(1, ONE)]) == 2
    assert conn.query_all("SELECT version, applied_at, description FROM schema_version ORDER BY version") == [
        (1, NOW, "fake 1"),
        (2, NOW, "fake 2"),
    ]
    assert {"t1", "t2"} <= tables(conn)


def test_database_at_one_applies_only_the_second(conn):
    migrate(conn, "earlier", [fake(1, ONE)])
    migrate(conn, NOW, [fake(1, ("CREATE TABLE never (id INTEGER)",)), fake(2, TWO)])
    assert "never" not in tables(conn)
    assert conn.query_all("SELECT version, applied_at FROM schema_version ORDER BY version") == [
        (1, "earlier"),
        (2, NOW),
    ]


def test_a_second_migrate_changes_nothing(conn):
    mods = [fake(1, ONE), fake(2, TWO)]
    migrate(conn, NOW, mods)
    before = conn.query_all("SELECT * FROM schema_version ORDER BY version")
    assert migrate(conn, "later", mods) == 2
    assert conn.query_all("SELECT * FROM schema_version ORDER BY version") == before
    assert conn.query_all("SELECT id FROM t2") == [(1,)]


def test_a_failing_statement_rolls_back_that_migration_and_keeps_version_one(conn):
    bad = fake(2, ("CREATE TABLE t2 (id INTEGER PRIMARY KEY)", "INSERT INTO missing_table VALUES (1)"))
    with pytest.raises(missing_table_error(conn), match="missing_table"):
        migrate(conn, NOW, [fake(1, ONE), bad])
    assert conn.query_all("SELECT version FROM schema_version") == [(1,)]
    assert "t2" not in tables(conn)
    assert "t1" in tables(conn)


class Interleaved(Connection):
    """Runs `hook` once, just before this connection's first BEGIN: another opener racing it."""

    def __init__(self, raw, hook):
        super().__init__(raw, SQLITE, begin="BEGIN IMMEDIATE")
        self.hook = hook

    @contextmanager
    def transaction(self):
        hook, self.hook = self.hook, lambda: None
        hook()
        with super().transaction() as c:
            yield c


# SQLite only: two file connections and BEGIN IMMEDIATE, which Postgres does not have.
def test_a_concurrent_opener_that_migrates_first_is_not_migrated_over(tmp_path):
    url = f"sqlite:///{tmp_path / 'race.db'}"
    mods = [fake(1, ONE), fake(2, TWO)]
    other = connect(url)
    racer = Interleaved(connect(url).raw, lambda: migrate(other, "other", mods))
    try:
        assert migrate(racer, NOW, mods) == 2
        assert racer.query_all("SELECT version, applied_at FROM schema_version ORDER BY version") == [
            (1, "other"),
            (2, "other"),
        ]
    finally:
        other.close()
        racer.close()


def test_a_gap_in_numbering_is_refused(conn):
    with pytest.raises(MigrationError, match="expected 2, found 3"):
        migrate(conn, NOW, [fake(1, ONE), fake(3, TWO)])
    assert "schema_version" not in tables(conn)


def test_a_duplicate_number_is_refused(conn):
    with pytest.raises(MigrationError, match="duplicate migration number 1"):
        migrate(conn, NOW, [fake(1, ONE), fake(1, TWO, name="harness.store_ddl_0001")])


def test_a_version_that_disagrees_with_its_name_is_refused(conn):
    with pytest.raises(MigrationError, match="VERSION 2"):
        migrate(conn, NOW, [fake(2, ONE, name="harness.store_ddl_0001")])


def test_pending_lists_the_migrations_in_version_order(conn):
    assert [v for v, _ in pending(conn, [fake(2, TWO), fake(1, ONE)])] == [1, 2]


def test_a_migration_named_by_its_dotted_path_is_imported(conn, monkeypatch):
    monkeypatch.setitem(sys.modules, "fakepkg.store_ddl_0001", fake(1, ONE, name="fakepkg.store_ddl_0001"))
    assert [v for v, _ in pending(conn, ["fakepkg.store_ddl_0001"])] == [1]


def test_a_database_newer_than_the_code_is_refused_before_any_write(conn, monkeypatch):
    migrate(conn, NOW, [fake(1, ONE), fake(2, TWO), fake(3, ("SELECT 1",))])
    executed = []

    def watch(name):
        real = getattr(conn, name)

        def spy(*args, **kwargs):
            executed.append(name)
            return real(*args, **kwargs)

        monkeypatch.setattr(conn, name, spy)

    for name in ("execute", "executemany", "transaction"):  # every way this connection writes
        watch(name)
    with pytest.raises(MigrationError) as err:
        migrate(conn, NOW, [fake(1, ONE), fake(2, TWO)])
    assert str(err.value) == ("database is at schema version 3, newest known migration is 2; "
                              "migration 0003 (fake 3) is not additive")
    assert executed == []


THREE = ("CREATE TABLE t3 (id INTEGER PRIMARY KEY)",)


def rows(c):
    return c.query_all("SELECT version, applied_at, description FROM schema_version ORDER BY version")


def test_accepts_equal_versions_and_refuses_an_older_store():
    assert accepts(2, 2, ())
    assert not accepts(1, 2, ())
    assert not accepts(0, 2, ())


def test_accepts_a_newer_store_only_when_every_newer_row_is_additive():
    assert accepts(4, 2, ("additive: t3", "additive: t4"))
    assert not accepts(4, 2, ("additive: t3", "t4"))
    assert not accepts(3, 2, ("t3",))
    assert not accepts(3, 2, ("Additive: t3",))  # the prefix is exact
    assert not accepts(3, 2, ("",))


def test_accepts_refuses_newer_rows_that_are_not_one_per_version():
    assert not accepts(4, 2, ("additive: t4",))
    assert not accepts(3, 2, ())


def test_newer_refusal_names_the_first_non_additive_migration_by_number_and_description():
    assert newer_refusal(2, ("additive: t3", "drops t1", "t5")) == "migration 0004 (drops t1) is not additive"
    assert "not one per version" in newer_refusal(2, ())


def test_recorded_description_prefixes_an_additive_migration_once():
    assert recorded_description("t3 table", True) == f"{ADDITIVE_PREFIX}t3 table"
    assert recorded_description("t3 table", False) == "t3 table"
    once = recorded_description("t3 table", True)
    assert recorded_description(once, True) == once


def test_migrate_records_an_additive_migration_with_the_prefix_and_only_a_true_flag_counts(conn):
    migrate(conn, NOW, [fake(1, ONE), fake(2, TWO, additive=True), fake(3, THREE, additive="yes")])
    assert [d for _, _, d in rows(conn)] == ["fake 1", "additive: fake 2", "fake 3"]


def test_a_store_newer_only_by_additive_migrations_is_accepted_and_left_as_it_is_every_time(conn):
    migrate(conn, NOW, [fake(1, ONE), fake(2, TWO), fake(3, THREE, additive=True)])
    before, tables_before = rows(conn), tables(conn)
    older = [fake(1, ONE), fake(2, TWO)]
    assert migrate(conn, "2026-09-25T00:00:00Z", older) == 3
    assert migrate(conn, "2026-09-26T00:00:00Z", older) == 3
    assert (rows(conn), tables(conn)) == (before, tables_before)
    assert newer_descriptions(conn, 2) == ("additive: fake 3",)


def test_a_store_newer_by_an_additive_then_a_non_additive_migration_is_refused_naming_the_second(conn):
    migrate(conn, NOW, [fake(1, ONE), fake(2, TWO, additive=True), fake(3, THREE)])
    before = rows(conn)
    with pytest.raises(MigrationError) as err:
        migrate(conn, NOW, [fake(1, ONE)])
    assert str(err.value) == ("database is at schema version 3, newest known migration is 1; "
                              "migration 0003 (fake 3) is not additive")
    assert rows(conn) == before


def test_newer_descriptions_is_empty_on_a_store_with_no_version_table(conn):
    assert newer_descriptions(conn, 0) == ()


def _store_one_newer(tmp_path, description):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    c = open_store(url, NOW)
    newest = len(default_modules())
    c.execute("INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
              (newest + 1, NOW, description))
    c.close()
    return url, newest


def test_the_pending_command_reports_a_store_newer_by_additive_migrations_as_up_to_date(tmp_path, capsys):
    url, newest = _store_one_newer(tmp_path, "additive: future table")
    assert store_migrate.main(["--pending", url]) == 0
    assert capsys.readouterr().out == (
        f"up to date (v{newest + 1}, newer than this code's v{newest} by additive migrations only)\n"
    )


def test_the_pending_command_refuses_a_store_newer_by_a_non_additive_migration(tmp_path, capsys):
    url, newest = _store_one_newer(tmp_path, "future table")
    assert store_migrate.main(["--pending", url]) == 2
    assert f"migration {newest + 1:04d} (future table) is not additive" in capsys.readouterr().err


def test_check_version_applies_nothing(conn):
    mods = [fake(1, ONE), fake(2, TWO)]
    assert check_version(conn, mods) == (0, 2)
    assert tables(conn) == set()
    migrate(conn, NOW, mods[:1])
    assert check_version(conn, mods) == (1, 2)
    assert conn.query_all("SELECT version FROM schema_version") == [(1,)]
    assert "t2" not in tables(conn)


def test_open_store_leaves_the_store_at_the_newest_real_migration(store_conn):
    newest = len(default_modules())
    assert check_version(store_conn) == (newest, newest)


def test_a_store_migrated_from_version_two_reads_an_existing_run_with_host_null(conn):
    real = default_modules()
    assert migrate(conn, NOW, real[:2]) == 2
    conn.execute("INSERT INTO runs (run_id, status) VALUES ('r1', 'done')")
    assert migrate(conn, NOW, real) == len(real)
    assert conn.query_all("SELECT run_id, status, host FROM runs") == [("r1", "done", None)]


def test_a_store_migrated_from_version_nine_reads_an_existing_run_and_lease_with_paused_and_status_null(conn):
    real = default_modules()
    assert migrate(conn, NOW, real[:9]) == 9
    conn.execute("INSERT INTO runs (run_id, status) VALUES ('r1', 'done')")
    conn.execute(
        "INSERT INTO leases (name, holder, epoch, heartbeat_at, expires_at) VALUES ('l1', 'h1', 1, 'T0', 'T1')"
    )
    assert migrate(conn, NOW, real) == len(real)
    assert conn.query_all("SELECT run_id, paused_at FROM runs") == [("r1", None)]
    assert conn.query_all("SELECT name, status FROM leases") == [("l1", None)]


def test_a_fresh_store_is_at_the_newest_version_and_runs_has_a_host_column(store_conn):
    newest = len(default_modules())
    assert check_version(store_conn) == (newest, newest)
    assert store_conn.query_all("SELECT host FROM runs") == []
    assert store_conn.query_all("SELECT paused_at FROM runs") == []
    assert store_conn.query_all("SELECT status FROM leases") == []


# SQLite only: the same check through a file path, which is how production opens its store.
def test_open_store_on_a_file_leaves_the_store_at_the_newest_real_migration(tmp_path):
    c = open_store(f"sqlite:///{tmp_path / 'x.db'}", NOW)
    try:
        newest = len(default_modules())
        assert check_version(c) == (newest, newest)
    finally:
        c.close()


class FakePG(Connection):
    """A postgres-shaped connection to a store at version 0 that holds a table, so a migration would back it up
    first; it refuses any write."""

    def __init__(self):
        super().__init__(raw=None, dialect=POSTGRES, begin="BEGIN")

    def query_all(self, sql, params=()):
        return [] if "schema_version" in sql else [("runs",)]

    def execute(self, sql, params=()):
        raise AssertionError("must not write when the backup step failed")

    def transaction(self):
        raise AssertionError("must not write when the backup step failed")

    def close(self):
        pass


def at_version_one(url):
    """A store at version 1 of the fake migrations, holding table t1: a store with something to back up."""
    c = connect(url)
    try:
        migrate(c, NOW, [fake(1, ONE)])
    finally:
        c.close()


@pytest.mark.parametrize(
    ("current", "newest", "has_tables", "backs_up"),
    [
        (0, 2, False, False),  # fresh: nothing to back up
        (0, 2, True, True),  # version 0 but holding a table
        (1, 2, False, True),
        (1, 2, True, True),
        (2, 2, True, False),  # nothing pending
        (3, 2, True, False),  # newer than the code
    ],
)
def test_should_back_up_only_a_store_that_holds_something_and_has_a_migration_pending(current, newest, has_tables, backs_up):
    assert should_back_up(current, newest, has_tables) is backs_up


def test_holds_tables_is_false_on_a_fresh_store_and_true_once_one_table_exists(conn):
    assert holds_tables(conn) is False
    migrate(conn, NOW, [fake(1, ONE)])
    assert holds_tables(conn) is True


def test_open_store_on_a_fresh_store_writes_no_backup_file(tmp_path, monkeypatch):
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(store_migrate, "default_modules", lambda: (fake(1, ONE),))
    open_store(f"sqlite:///{tmp_path / 'x.db'}", NOW).close()
    assert not (tmp_path / "backups").exists()


def test_open_store_backs_up_the_sqlite_file_before_the_new_table_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    url = f"sqlite:///{tmp_path / 'x.db'}"
    at_version_one(url)
    monkeypatch.setattr(store_migrate, "default_modules", lambda: (fake(1, ONE), fake(2, TWO)))
    open_store(url, NOW).close()
    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert [b.name for b in backups] == ["store-v1-to-v2-20260924T000000Z.sqlite"]
    backup = sqlite3.connect(str(backups[0]))
    try:
        assert {r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == {
            "schema_version", "t1"}
    finally:
        backup.close()


def test_open_store_with_nothing_pending_writes_no_backup_file(tmp_path, monkeypatch):
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    url = f"sqlite:///{tmp_path / 'x.db'}"
    at_version_one(url)
    monkeypatch.setattr(store_migrate, "default_modules", lambda: (fake(1, ONE), fake(2, TWO)))
    open_store(url, NOW).close()
    after_first_open = list((tmp_path / "backups").glob("*"))
    assert len(after_first_open) == 1
    open_store(url, NOW).close()
    assert list((tmp_path / "backups").glob("*")) == after_first_open


def test_a_missing_pg_dump_raises_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: pg_dump is not on it
    monkeypatch.setattr(store_migrate, "connect", lambda url: FakePG())
    with pytest.raises(MigrationError, match="pg_dump"):
        open_store("postgresql://user:secret@host/db", NOW)


def test_the_without_backup_override_skips_the_backup_and_prints_one_warning_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COX_STORE_MIGRATE_WITHOUT_BACKUP", "1")
    monkeypatch.setenv("COX_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    url = f"sqlite:///{tmp_path / 'x.db'}"
    at_version_one(url)
    monkeypatch.setattr(store_migrate, "default_modules", lambda: (fake(1, ONE), fake(2, TWO)))
    open_store(url, NOW).close()
    assert not (tmp_path / "backups").exists()
    assert capsys.readouterr().err.count("\n") == 1


def test_pending_lists_exactly_the_migrations_newer_than_current(conn):
    migrate(conn, NOW, [fake(1, ONE)])
    assert pending(conn, [fake(1, ONE), fake(2, TWO), fake(3, ("SELECT 1",))]) == [(2, "fake 2"), (3, "fake 3")]


def test_a_password_in_the_url_never_appears_in_a_raised_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: pg_dump is not on it
    monkeypatch.setattr(store_migrate, "connect", lambda url: FakePG())
    url = "postgresql://user:s3cret-pw@host/db"
    with pytest.raises(MigrationError) as err:
        open_store(url, NOW)
    assert "s3cret-pw" not in str(err.value)


@pytest.mark.parametrize("module", default_modules(), ids=lambda m: f"{m.VERSION:04d}")
@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_every_default_module_statement_is_portable_and_has_no_foreign_key(module, dialect):
    sql = module.statements(dialect)
    assert [forbidden_constructs(s) for s in sql] == [()] * len(sql)
    assert not [s for s in sql if "REFERENCES" in s.upper() or "FOREIGN KEY" in s.upper()]


# Every ADDITIVE_EXEMPT a default module declares, by version, with its reason. A module that adds one must add it here
# too, so a reviewer reads the exemption and its reason in the diff.
KNOWN_EXEMPTIONS: dict[int, dict[str, str]] = {
    21: {
        "trigger": (
            "Each CREATE TRIGGER is an AFTER ... FOR EACH STATEMENT trigger. It fires once the statement's rows "
            "are written, and Postgres ignores what a statement-level trigger returns, so it cannot skip, drop or"
            " rewrite a row. Its function writes no row and takes no row lock: the only lock NOTIFY adds is "
            "Postgres's short queue lock at commit, taken after every lock the writer holds, so it cannot "
            "deadlock an older writer either. The one way it can fail an older writer is a full notify queue (a "
            "listener that never drains): every writer to a watched table then fails at commit with 54000. The "
            "default 8 GB queue makes that remote."
        ),
        "function": (
            "cox_notify has no arguments, returns NULL, and runs one statement: pg_catalog.pg_notify('cox_store',"
            " TG_TABLE_NAME). It reads and writes no table. It runs with the invoker's rights (no SECURITY "
            "DEFINER), its search_path is pinned, and EXECUTE is revoked from PUBLIC so no other role can attach "
            "it to a table."
        ),
    },
    23: {
        "trigger": (
            "The one CREATE OR REPLACE TRIGGER replaces 0021's cox_notify trigger on workers with the same AFTER ... "
            "FOR EACH STATEMENT trigger calling the same function, its UPDATE OF list one column longer (machine_id). "
            "As 0021's, it fires once the statement's rows are written and Postgres ignores what it returns, so it "
            "cannot skip, drop or rewrite an older writer's row; an older writer's statements fire it exactly as "
            "before."
        ),
    },
}


def test_every_additive_exemption_is_listed_here():
    found = {m.VERSION: dict(m.ADDITIVE_EXEMPT) for m in default_modules() if hasattr(m, "ADDITIVE_EXEMPT")}
    assert found == KNOWN_EXEMPTIONS


@pytest.mark.parametrize("module", default_modules(), ids=lambda m: f"{m.VERSION:04d}")
@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_every_default_module_that_declares_additive_holds_only_additive_ddl(module, dialect):
    declared = getattr(module, "ADDITIVE", False) is True
    exempt = getattr(module, "ADDITIVE_EXEMPT", {})
    assert (additive_violations(module.statements(dialect), exempt) if declared else ()) == ()


ADDITIVE_DDL = (
    "CREATE TABLE IF NOT EXISTS notes (key TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (key))",
    'CREATE TABLE IF NOT EXISTS public."marks" (key TEXT NOT NULL, PRIMARY KEY (key))',
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_notes_body ON notes (body)",
    'CREATE UNIQUE INDEX ux_marks ON public."marks" (key)',
    "CREATE INDEX IF NOT EXISTS ix_runs_host ON runs (host)",
    "ALTER TABLE hosts ADD COLUMN lanes INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE hosts ADD COLUMN IF NOT EXISTS labels TEXT NOT NULL DEFAULT '', ADD COLUMN note TEXT",
    'ALTER TABLE public."hosts" ADD COLUMN "zone" TEXT NOT NULL DEFAULT \'\'',
    "-- DROP is only named in this comment\nINSERT INTO notes (key, body) VALUES ('drop', 'RENAME me')",
    "INSERT INTO notes (key, body) VALUES ('--', 'UPDATE runs SET x = 1')",
    "CREATE INDEX IF NOT EXISTS ix_notes_key ON notes (key);",
    "INSERT INTO notes (key, body) VALUES ('a;b', $$c; d$$), ('e', $q$f;$q$)",
    "INSERT INTO notes (key, body) VALUES ('type', 'E')",
)
NOTES = "CREATE TABLE IF NOT EXISTS notes (key TEXT NOT NULL, PRIMARY KEY (key))"
TRIGGER = "CREATE TRIGGER IF NOT EXISTS tr_runs AFTER INSERT ON runs BEGIN INSERT INTO notes VALUES (NEW.run_id); END"
FUNCTION = ("CREATE OR REPLACE FUNCTION note_run() RETURNS trigger AS $$ BEGIN INSERT INTO notes VALUES (NEW.run_id); "
            "RETURN NEW; END $$ LANGUAGE plpgsql")
WHY = "AFTER INSERT, inserts into notes only: never drops or rewrites the row an older writer inserted"


def test_additive_ddl_has_no_violation():
    assert additive_violations(ADDITIVE_DDL, {}) == ()


@pytest.mark.parametrize(("sql", "reason"), [
    pytest.param("DROP TABLE notes", "has DROP", id="drop-table"),
    pytest.param("ALTER TABLE runs DROP COLUMN host", "has DROP", id="drop-column"),
    pytest.param("ALTER TABLE runs RENAME COLUMN host TO machine", "has RENAME", id="rename-column"),
    pytest.param("ALTER TABLE runs RENAME TO old_runs", "has RENAME", id="rename-table"),
    pytest.param("ALTER TABLE runs ALTER COLUMN host TYPE INTEGER", "has ALTER COLUMN", id="alter-column-type"),
    pytest.param("ALTER TABLE runs ALTER host TYPE INTEGER", "by more than adding a column", id="alter-type-bare"),
    pytest.param("ALTER TABLE runs ADD COLUMN lanes INTEGER NOT NULL", "NOT NULL column to runs with no DEFAULT",
                 id="not-null-no-default"),
    pytest.param("ALTER TABLE runs ADD COLUMN a TEXT, ADD COLUMN b TEXT NOT NULL", "with no DEFAULT", id="second-action"),
    pytest.param("ALTER TABLE runs ADD CONSTRAINT one_host UNIQUE (host)", "by more than adding a column",
                 id="add-constraint"),
    pytest.param("ALTER TABLE runs ADD COLUMN code TEXT UNIQUE", "with a constraint", id="unique-column"),
    pytest.param("CREATE UNIQUE INDEX ux_runs_host ON runs (host)", "unique index to runs", id="unique-index-old-table"),
    pytest.param("DELETE FROM runs", "has DELETE", id="delete"),
    pytest.param("TRUNCATE runs", "has TRUNCATE", id="truncate"),
    pytest.param("CREATE TRIGGER t AFTER INSERT ON runs BEGIN DELETE FROM notes; END", "has DELETE",
                 id="trigger-body-deletes"),
    pytest.param("REVOKE EXECUTE ON FUNCTION note_run() FROM PUBLIC", "a function it did not create",
                 id="revoke-on-an-older-function"),
    pytest.param("REVOKE SELECT ON TABLE runs FROM PUBLIC", "a REVOKE this check cannot read", id="revoke-on-a-table"),
    pytest.param("REVOKE EXECUTE ON FUNCTION f(int) FROM PUBLIC", "a REVOKE this check cannot read",
                 id="revoke-with-arguments"),
    pytest.param("REVOKE EXECUTE ON FUNCTION f() FROM cox_worker", "a REVOKE this check cannot read",
                 id="revoke-from-a-role"),
    pytest.param("CREATE TRIGGER t BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT, 'no'); END", "has RAISE",
                 id="raising-trigger"),
    # Quoted and schema-qualified names are read, not skipped.
    pytest.param('ALTER TABLE "hosts" ADD COLUMN x TEXT NOT NULL', "NOT NULL column to hosts with no DEFAULT",
                 id="quoted-alter"),
    pytest.param("ALTER TABLE public.hosts ADD COLUMN x TEXT NOT NULL", "NOT NULL column to hosts with no DEFAULT",
                 id="qualified-alter"),
    pytest.param('CREATE UNIQUE INDEX u ON "hosts" (name)', "unique index to hosts", id="quoted-unique-index"),
    pytest.param('CREATE UNIQUE INDEX u ON public."hosts" (name)', "unique index to hosts", id="qualified-unique-index"),
    # What the check cannot read fails closed.
    pytest.param('ALTER TABLE "odd name" ADD COLUMN x TEXT', "ALTER TABLE this check cannot read", id="unreadable-alter"),
    pytest.param('CREATE UNIQUE INDEX u ON "odd name" (x)', "CREATE UNIQUE INDEX this check cannot read",
                 id="unreadable-unique-index"),
    pytest.param("CREATE UNIQUE INDEX u ON hosts USING btree (name)", "CREATE UNIQUE INDEX this check cannot read",
                 id="unique-index-using"),
    pytest.param("CREATE VIEW v AS SELECT 1", "not a kind of statement this check reads", id="unknown-kind"),
    pytest.param("ALTER TABLE hosts ADD COLUMN", "by more than adding a column, or in a way", id="truncated-alter"),
    # Data rewrites, triggers and functions need a stated exemption.
    pytest.param("UPDATE hosts SET state = 'gone'", "has an UPDATE", id="update"),
    pytest.param('UPDATE "odd name" SET x = 1', "UPDATE this check cannot read", id="unreadable-update"),
    pytest.param("INSERT INTO notes SELECT key FROM hosts; UPDATE OR IGNORE hosts SET state = ''", "has an UPDATE",
                 id="update-or-ignore"),
    pytest.param(TRIGGER, "creates a trigger, which ADDITIVE_EXEMPT does not exempt", id="trigger"),
    pytest.param("CREATE TRIGGER t BEFORE INSERT ON hosts FOR EACH ROW EXECUTE FUNCTION f()", "creates a trigger",
                 id="before-trigger"),
    pytest.param(FUNCTION, "creates a function, which ADDITIVE_EXEMPT does not exempt", id="or-replace-function"),
    # A quote inside a comment opens no literal, so it cannot hide what follows it.
    pytest.param("ALTER TABLE hosts ADD COLUMN a TEXT DEFAULT 'x' -- isn't\n, ADD COLUMN b TEXT NOT NULL, "
                 "ADD COLUMN c TEXT DEFAULT 'y'", "NOT NULL column to hosts with no DEFAULT", id="quote-in-comment"),
    pytest.param("ALTER TABLE hosts ADD COLUMN a TEXT NOT NULL DEFAULT '--', ADD COLUMN b TEXT NOT NULL",
                 "NOT NULL column to hosts with no DEFAULT", id="dashes-in-literal"),
    # One string, one statement: a `;` cannot hide a second one, which Postgres would run.
    pytest.param("CREATE INDEX IF NOT EXISTS ix ON hosts (a); "
                 "CREATE TRIGGER t BEFORE INSERT ON hosts FOR EACH ROW EXECUTE FUNCTION f()",
                 "several statements in one string; split them", id="index-then-trigger"),
    pytest.param("CREATE INDEX IF NOT EXISTS ix ON hosts (a); CREATE UNIQUE INDEX u ON hosts (name)",
                 "several statements in one string", id="index-then-unique-index"),
    pytest.param("CREATE INDEX ix ON hosts (a);;", "several statements in one string", id="two-trailing"),
    pytest.param("INSERT INTO notes VALUES ($$x$$); DROP TABLE hosts", "several statements in one string",
                 id="after-a-dollar-literal"),
    pytest.param(TRIGGER + "; CREATE UNIQUE INDEX u ON hosts (name)", "several statements in one string",
                 id="after-a-trigger-body"),
    pytest.param(FUNCTION + "; ALTER TABLE hosts ADD COLUMN b TEXT NOT NULL", "several statements in one string",
                 id="after-a-function-body"),
    pytest.param("INSERT INTO notes VALUES ($tag$ $$; $tag$); CREATE UNIQUE INDEX u ON hosts (name)",
                 "several statements in one string", id="tagged-dollar-holding-dollars"),
    pytest.param("CREATE FUNCTION g() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END; "
                 "CREATE VIEW v AS SELECT CASE WHEN true THEN 1 END", "several statements in one string",
                 id="after-an-atomic-body-ending-in-case-end"),
    pytest.param("CREATE TRIGGER t AFTER INSERT ON runs BEGIN INSERT INTO notes VALUES (NEW.run_id); END; "
                 "CREATE VIEW v AS SELECT CASE WHEN 1 THEN 1 END", "several statements in one string",
                 id="after-a-sqlite-trigger-body-ending-in-case-end"),
    pytest.param("CREATE TRIGGER t AFTER INSERT ON runs BEGIN INSERT INTO notes VALUES (NEW.run_id); "
                 "INSERT INTO notes VALUES (NEW.host);", "several statements in one string", id="unclosed-trigger-body"),
    # A Postgres escape string's backslashes are not read, so it is refused rather than misread.
    pytest.param("ALTER TABLE hosts ADD COLUMN a TEXT DEFAULT E'\\'', ADD COLUMN b TEXT NOT NULL",
                 "escape-string literal", id="escape-string"),
    pytest.param("INSERT INTO notes VALUES (e'x')", "escape-string literal", id="lower-escape-string"),
])
def test_a_non_additive_statement_is_named(sql, reason):
    found = additive_violations((NOTES, sql), {})
    assert found and all(r.startswith("statement 2 ") for r in found) and any(reason in r for r in found), found


def test_a_trigger_or_function_body_may_hold_semicolons_and_is_still_scanned():
    assert additive_violations((NOTES, TRIGGER, FUNCTION + ";"), {"trigger": WHY, "function": WHY}) == ()
    nested = ("CREATE TRIGGER t2 AFTER INSERT ON runs BEGIN INSERT INTO notes VALUES "
              "(CASE WHEN NEW.run_id = '' THEN 'x' ELSE NEW.run_id END); END;")
    atomic = "CREATE FUNCTION g() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT CASE WHEN true THEN 1 END; END"
    assert additive_violations((NOTES, nested, atomic), {"trigger": WHY, "function": WHY}) == ()
    hidden = FUNCTION.replace("RETURN NEW;", "DELETE FROM notes; RETURN NEW;")
    assert additive_violations((NOTES, hidden), {"function": WHY}) == ("statement 2 has DELETE",)


def test_truncate_as_a_trigger_event_and_a_revoke_on_its_own_new_function_are_additive():
    trigger = ("CREATE TRIGGER t AFTER INSERT OR DELETE OR TRUNCATE OR UPDATE OF a, b ON runs "
               "FOR EACH STATEMENT EXECUTE FUNCTION note_run()")
    revoke = "REVOKE EXECUTE ON FUNCTION note_run() FROM PUBLIC"
    assert additive_violations((NOTES, FUNCTION, revoke, trigger), {"trigger": WHY, "function": WHY}) == ()
    # A revoke names the function as created, schema and all: another schema's `note_run` is not this one.
    other = "REVOKE EXECUTE ON FUNCTION other.note_run() FROM PUBLIC"
    assert additive_violations((NOTES, FUNCTION, other), {"function": WHY}) == (
        "statement 3 revokes EXECUTE on other.note_run, a function it did not create",
    )
    qualified = FUNCTION.replace("FUNCTION note_run()", "FUNCTION public.note_run()")
    assert additive_violations((NOTES, qualified, revoke), {"function": WHY}) == (
        "statement 3 revokes EXECUTE on note_run, a function it did not create",
    )
    assert additive_violations((NOTES, qualified, revoke.replace("note_run", "public.note_run")),
                               {"function": WHY}) == ()
    # The event list is read only in its place: a TRUNCATE in a trigger's body still counts.
    body = "CREATE TRIGGER t AFTER TRUNCATE ON runs BEGIN TRUNCATE notes; END"
    assert additive_violations((NOTES, body), {"trigger": WHY}) == ("statement 2 has TRUNCATE",)


def test_a_trigger_function_and_update_pass_only_when_exempted_with_a_reason():
    statements = (NOTES, TRIGGER, FUNCTION, "UPDATE hosts SET labels = '' WHERE labels IS NULL")
    assert additive_violations(statements, {"trigger": WHY, "function": WHY, "update": "backfills labels only"}) == ()
    assert additive_violations(statements, {"trigger": WHY, "function": WHY}) == (
        "statement 4 has an UPDATE, which ADDITIVE_EXEMPT does not exempt",
    )
    # An exemption never lifts the bans that hold everywhere.
    assert additive_violations((NOTES, "CREATE TRIGGER t AFTER INSERT ON runs BEGIN DELETE FROM notes; END"),
                               {"trigger": WHY}) == ("statement 2 has DELETE",)


@pytest.mark.parametrize(("exempt", "reason"), [
    pytest.param({"trigger": ""}, "exempts trigger with no reason", id="empty-reason"),
    pytest.param({"trigger": "   "}, "exempts trigger with no reason", id="blank-reason"),
    pytest.param({"trigger": True}, "exempts trigger with no reason", id="not-text"),
    pytest.param({"drop": WHY}, "exempts 'drop', which is not one of trigger, function, update", id="unknown-kind"),
])
def test_an_exemption_without_a_reason_or_of_an_unknown_kind_is_a_violation(exempt, reason):
    found = additive_violations((NOTES, TRIGGER), exempt)
    assert reason in found, found


def test_a_module_declaring_additive_with_non_additive_ddl_is_refused_before_any_write(conn):
    migrate(conn, NOW, [fake(1, ONE)])
    before = rows(conn)
    with pytest.raises(MigrationError, match=r"migration 0002 declares ADDITIVE but is not: statement 1 has DROP"):
        migrate(conn, NOW, [fake(1, ONE), fake(2, ("DROP TABLE t1",), additive=True)])
    assert rows(conn) == before and "t1" in tables(conn)


def test_a_trigger_migration_applies_only_with_its_exemption(conn):
    trigger = "CREATE TRIGGER IF NOT EXISTS tr_t1 AFTER INSERT ON t1 BEGIN INSERT INTO t2 (id) VALUES (NEW.id); END"
    plain = fake(2, ("CREATE TABLE t2 (id INTEGER PRIMARY KEY)", trigger), additive=True)
    migrate(conn, NOW, [fake(1, ONE)])
    if conn.dialect is POSTGRES:
        pytest.skip("the trigger statement is sqlite's; the exemption rule itself is dialect-free")
    with pytest.raises(MigrationError, match="statement 2 creates a trigger, which ADDITIVE_EXEMPT does not exempt"):
        migrate(conn, NOW, [fake(1, ONE), plain])
    plain.ADDITIVE_EXEMPT = {"trigger": "AFTER INSERT into a new table only: the t1 row is kept as written"}
    assert migrate(conn, NOW, [fake(1, ONE), plain]) == 2
    conn.execute("INSERT INTO t1 (id) VALUES (7)")
    assert conn.query_all("SELECT id FROM t2") == [(7,)]


def _and_a_newer_build_commits(description):
    """Migration 2's statements, then the row a newer build's migration 3 would commit before this opener reads the
    version back: the window between this opener's last migration and its final read."""
    return (*TWO, f"INSERT INTO schema_version (version, applied_at, description) VALUES (3, '{NOW}', '{description}')")


def test_a_newer_build_that_migrates_additively_while_this_one_migrates_is_accepted(conn):
    migrate(conn, NOW, [fake(1, ONE)])
    assert migrate(conn, NOW, [fake(1, ONE), fake(2, _and_a_newer_build_commits("additive: fake 3"))]) == 3


def test_a_newer_build_that_migrates_non_additively_while_this_one_migrates_is_refused(conn):
    migrate(conn, NOW, [fake(1, ONE)])
    with pytest.raises(MigrationError, match=r"newest known migration is 2; migration 0003 \(fake 3\) is not additive"):
        migrate(conn, NOW, [fake(1, ONE), fake(2, _and_a_newer_build_commits("fake 3"))])


@pytest.mark.parametrize(
    ("url", "dbname", "password"),
    [
        ("postgresql://cox:s3cret-pw@db.host:5433/store", "postgresql://cox@db.host:5433/store", "s3cret-pw"),
        ("postgresql+psycopg://cox:p%40ss%3Aw@[::1]:5432/store?sslmode=require",
         "postgresql://cox@[::1]:5432/store?sslmode=require", "p@ss:w"),
        ("postgresql://cox@h/store?password=s3cret-pw&options=-c%20search_path%3Dx",
         "postgresql://cox@h/store?options=-c%20search_path%3Dx", "s3cret-pw"),
        ("postgresql://h/store", "postgresql://h/store", None),
        ("postgresql://cox:pw@/store?host=/var/run/postgresql", "postgresql://cox@/store?host=/var/run/postgresql", "pw"),
        ("postgresql:///store?host=/var/run/postgresql&password=pw", "postgresql:///store?host=/var/run/postgresql", "pw"),
        ("postgresql://cox:pw@%2Fvar%2Frun%2Fpostgresql/store", "postgresql://cox@%2Fvar%2Frun%2Fpostgresql/store", "pw"),
        ("postgres+psycopg://cox:pw@h:5432/store", "postgres://cox@h:5432/store", "pw"),
    ],
    ids=["userinfo", "encoded-driver-tag-ipv6", "query", "none", "socket-query-host", "socket-no-user",
         "socket-encoded-host", "postgres-driver-tag"],
)
def test_pg_dump_command_keeps_the_password_out_of_argv_and_in_pgpassword(url, dbname, password, tmp_path):
    argv, env = store_migrate.pg_dump_command(url, tmp_path / "x.dump")
    assert argv == ["pg_dump", "-Fc", f"--dbname={dbname}", "-f", str(tmp_path / "x.dump")]
    assert env == ({"PGPASSWORD": password} if password else {})
    assert not any("s3cret" in a or "p@ss" in a or "p%40ss" in a or ":pw@" in a or "password=" in a for a in argv)
