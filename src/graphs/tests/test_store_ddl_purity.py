"""Every `store_ddl_NNNN` migration is schema only and a pure function of the dialect, unless it says otherwise.

A migration that writes rows (INSERT, UPDATE, DELETE, MERGE, TRUNCATE, COPY, CREATE TABLE ... AS SELECT) changes data
on every store it opens, and one that reads the environment or the clock migrates two stores differently. Either may be right for a migration, but only on
purpose: it then declares `WRITES_DATA = "<why>"` or `READS_ENV_OR_TIME = "<why>"`, a non-empty reason, and this guard
lets it through. 0021 declares WRITES_DATA (the cursor's seed rows and its trigger function); none declares
READS_ENV_OR_TIME.
"""

import ast
import inspect
import re

import pytest

from harness.store_dialect import POSTGRES, SQLITE
from harness.store_migrate import default_modules

MODULES = default_modules()
WRITE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|COPY)\b"
    r"|\bCREATE\s+(?:\w+\s+)*?TABLE\b[^;]*?\bAS\s*\(?\s*(?:SELECT|WITH|VALUES|TABLE)\b",  # CREATE TABLE ... AS SELECT
    re.IGNORECASE,
)
# What reads the environment or the clock: the modules that do it, and the names they are reached by.
IMPURE_MODULES = frozenset({"os", "sys", "time", "datetime", "calendar", "zoneinfo", "random", "uuid", "secrets"})
IMPURE_NAMES = frozenset({"environ", "getenv", "now", "utcnow", "today", "time", "monotonic"})


def code_only(sql: str) -> str:
    """Pure. `sql` without its quoted literals and comments, so a default of 'update' or a note is not a statement."""
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", " ", sql, flags=re.DOTALL)


def writes(statements: tuple[str, ...]) -> tuple[str, ...]:
    """Pure. One line per statement that writes rows."""
    found = ((i, WRITE.search(code_only(s))) for i, s in enumerate(statements))
    return tuple(f"statement {i}: {(m.group(1) or 'CREATE TABLE AS').upper()}" for i, m in found if m)


def impure_reads(source: str) -> tuple[str, ...]:
    """Pure. One line per import of, or name for, something that reads the environment or the clock."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"imports {a.name}" for a in node.names if a.name.split(".")[0] in IMPURE_MODULES]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in IMPURE_MODULES:
            found.append(f"imports from {node.module}")
        elif isinstance(node, ast.Attribute) and node.attr in IMPURE_NAMES:
            found.append(f"uses .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in IMPURE_NAMES:
            found.append(f"uses {node.id}")
    return tuple(found)


def declared(module, name: str) -> bool:
    why = getattr(module, name, None)
    return isinstance(why, str) and bool(why.strip())


@pytest.mark.parametrize("sql", [
    "INSERT INTO t (id) VALUES (1)",
    "update t SET a = 1",
    "CREATE TRIGGER x AFTER INSERT ON t BEGIN DELETE FROM u; END",
    "TRUNCATE TABLE t",
    "COPY t FROM '/tmp/rows.csv'",
    "CREATE TABLE t2 AS SELECT * FROM t",
    "CREATE TEMP TABLE IF NOT EXISTS t2 AS (WITH x AS (SELECT 1) SELECT * FROM x)",
])
def test_a_statement_that_writes_rows_is_named(sql):
    assert writes((sql,)) != ()


@pytest.mark.parametrize("sql", [
    "CREATE TABLE t (updated_at TEXT NOT NULL DEFAULT 'update', inserted_by TEXT)",
    "ALTER TABLE t ADD COLUMN note TEXT NOT NULL DEFAULT ''  -- never updated",
    "CREATE INDEX t_by_deleted ON t (deleted_at)",
    "CREATE TABLE copies (copied_at TEXT, truncated INTEGER, PRIMARY KEY (copied_at))",
    "CREATE TABLE t (a TEXT GENERATED ALWAYS AS (upper(b)) STORED, b TEXT)",
])
def test_schema_only_ddl_is_not_a_write(sql):
    assert writes((sql,)) == ()


@pytest.mark.parametrize("source", [
    "import os\nX = os.environ['A']",
    "from datetime import datetime\nX = datetime.now()",
    "import time\nX = time.time()",
    "X = getenv('A')",
])
def test_a_read_of_the_environment_or_the_clock_is_named(source):
    assert impure_reads(source) != ()


def test_a_migration_built_from_the_dialect_alone_reads_nothing():
    assert impure_reads("from harness.store_dialect import Dialect\ndef statements(d):\n    return (d.name,)") == ()


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_a_migration_writes_no_rows_unless_it_declares_why(module):
    if declared(module, "WRITES_DATA"):
        return
    found = writes(module.statements(SQLITE)) + writes(module.statements(POSTGRES))
    assert found == (), f"{module.__name__} writes rows; declare WRITES_DATA = '<why>' if it must: {found}"


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_a_migration_reads_no_environment_or_clock_unless_it_declares_why(module):
    if declared(module, "READS_ENV_OR_TIME"):
        return
    found = impure_reads(inspect.getsource(module))
    assert found == (), f"{module.__name__} reads the environment or the clock; declare READS_ENV_OR_TIME: {found}"


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_a_migration_gives_the_same_statements_every_time(module):
    for dialect in (SQLITE, POSTGRES):
        assert module.statements(dialect) == module.statements(dialect)
