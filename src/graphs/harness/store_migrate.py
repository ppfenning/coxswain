"""Numbered schema migrations, applied when a store is opened.

A migration is a module in the harness package named `store_ddl_NNNN` (four
digits, contiguous from 0001) exposing `VERSION`, `DESCRIPTION` and
`statements(dialect)`. Adding one never edits this file: `default_modules`
finds them with pkgutil. Only `open_store` applies migrations. Read paths call
`check_version`, which never writes.

A migration that only adds (tables, columns, indexes, triggers) may declare `ADDITIVE = True`. `migrate` records its
`schema_version` description as `additive: <DESCRIPTION>`, and code older than it then still opens the store: a store
newer than the code is accepted when every `schema_version` row above the code's newest migration carries that prefix
(`accepts`), and refused, naming the first row that does not, otherwise. That lets an additive migration apply while
processes on the previous build (a lane's heartbeat, say) keep running.
"""

from __future__ import annotations

import argparse
import importlib
import os
import pkgutil
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import cast
from urllib.parse import unquote, urlsplit

import harness
from harness.store_dialect import Connection, Dialect, connect

__all__ = [
    "ADDITIVE_PREFIX",
    "EXEMPTABLE",
    "MigrationError",
    "NotExactVersion",
    "accepts",
    "additive_violations",
    "check_version",
    "default_modules",
    "exact_refusal",
    "holds_tables",
    "migrate",
    "newer_descriptions",
    "newer_refusal",
    "open_store",
    "open_store_exact",
    "pending",
    "recorded_description",
    "should_back_up",
]

_WITHOUT_BACKUP = "COX_STORE_MIGRATE_WITHOUT_BACKUP"
_BACKUP_DIR_ENV = "COX_STORE_BACKUP_DIR"

_NAME = re.compile(r"(?:^|\.)store_ddl_(\d{4})$")
_CREATE = "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT, description TEXT)"

Migration = tuple[int, Callable[[Dialect], tuple[str, ...]]]

ADDITIVE_PREFIX = "additive: "


class MigrationError(RuntimeError):
    """The migration set or the database is not in a state the runner will act on."""


class NotExactVersion(MigrationError):
    """A whole-schema consumer (a copy, a switch) was given a store not at exactly this code's version. The message
    names the role and the versions only, never the store's URL."""


def _ordered(given: Sequence[ModuleType | str]) -> tuple[ModuleType, ...]:
    """Modules, or dotted names to import, sorted by VERSION, each matching its name, contiguous from 1."""
    modules = tuple(importlib.import_module(g) if isinstance(g, str) else g for g in given)
    for m in modules:
        found = _NAME.search(m.__name__)
        if found is None:
            raise MigrationError(f"{m.__name__} is not named store_ddl_NNNN")
        named = int(found.group(1))
        if named != m.VERSION:
            raise MigrationError(f"{m.__name__} declares VERSION {m.VERSION}, its name says {named}")
    ordered = tuple(sorted(modules, key=lambda m: m.VERSION))
    versions = [m.VERSION for m in ordered]
    dupes = sorted({v for v in versions if versions.count(v) > 1})
    if dupes:
        raise MigrationError(f"duplicate migration number {dupes[0]}")
    gaps = [(want, got) for want, got in zip(range(1, len(versions) + 1), versions) if want != got]
    if gaps:
        raise MigrationError(f"migration numbering has a gap: expected {gaps[0][0]}, found {gaps[0][1]}")
    return ordered


def default_modules() -> tuple[ModuleType, ...]:
    """Every store_ddl_NNNN module found in the harness package."""
    names = sorted(i.name for i in pkgutil.iter_modules(harness.__path__) if re.fullmatch(r"store_ddl_\d{4}", i.name))
    return tuple(importlib.import_module(f"harness.{n}") for n in names)


def _has_version_table(conn: Connection) -> bool:
    # unknown: the postgres branch is not run in this repository's tests.
    if conn.dialect.name == "sqlite":
        sql = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    else:
        sql = "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_version' AND table_schema = current_schema()"
    return conn.query_one(sql) is not None


def holds_tables(conn: Connection) -> bool:
    """Edge, reads only. Whether the store's schema holds any table at all; a store just created holds none."""
    if conn.dialect.name == "sqlite":
        sql = "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
    else:
        sql = "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() LIMIT 1"
    return conn.query_one(sql) is not None


def _current_version(conn: Connection) -> int:
    if not _has_version_table(conn):
        return 0
    row = conn.query_one("SELECT MAX(version) FROM schema_version")
    return 0 if row is None or row[0] is None else int(row[0])


def _lock_versions(dialect: Dialect) -> tuple[str, ...]:
    # sqlite's BEGIN IMMEDIATE already holds the write lock. Postgres BEGIN takes none,
    # so a second opener would read schema_version before the first commits.
    return ("LOCK TABLE schema_version IN SHARE ROW EXCLUSIVE MODE",) if dialect.name == "postgres" else ()


def recorded_description(description: str, additive: bool) -> str:
    """Pure. The `schema_version` description a migration is recorded with: `additive: <description>` when it declares
    `ADDITIVE = True`, else the description as given. A description that already carries the prefix keeps one."""
    return ADDITIVE_PREFIX + description if additive and not description.startswith(ADDITIVE_PREFIX) else description


def _declares_additive(m: ModuleType) -> bool:
    """Whether the module sets `ADDITIVE = True`; anything else, absence included, is not additive."""
    return getattr(m, "ADDITIVE", False) is True


# One left-to-right pass, so whichever opens first wins: a quote inside a comment (`-- isn't`) is comment, and `--`
# inside a string literal is literal. Literals become '', comments a space, quoted identifiers their bare name, a
# dollar-quoted body ($$...$$ or $tag$...$tag$) its own tokenized text between two markers, and a Postgres escape
# string (E'...'), whose backslash escapes this pass does not read, a marker that is always a violation.
_TOKEN = re.compile(
    r"""(?P<escape>(?<!\w)[Ee]')"""
    r"""|'(?:[^']|'')*'"""
    r'|"(?P<quoted>(?:[^"]|"")*)"'
    r"""|\$(?P<tag>(?:[A-Za-z_]\w*)?)\$(?P<body>.*?)\$(?P=tag)\$"""
    r"""|--[^\n]*|/\*.*?\*/""",
    re.DOTALL,
)
_UNREADABLE = "?"  # what a quoted identifier that is not a plain word becomes: no pattern below reads it
_ESCAPE = "\x02"  # what an E'...' literal's opening becomes
_OPEN, _CLOSE = "\x00", "\x01"  # around a dollar-quoted body
_BODY = re.compile(f"{_OPEN}.*?{_CLOSE}", re.DOTALL)
_BLOCK_WORD = re.compile(r"\b(BEGIN|CASE|END)\b")  # what opens and closes a trigger's or function's body
_TABLE = r"(?:\w+\.)?(\w+)"  # an optionally schema-qualified table name; group: the table
_ALTER = re.compile(rf"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_TABLE}\s+(\S.*)$", re.DOTALL)
_CREATED = re.compile(rf"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_TABLE}\s*\(")
# A function's name as written, schema and all: `other.f` is not the `f` this migration created.
_QUALIFIED = r"((?:\w+\.)?\w+)"
_CREATED_FUNCTION = re.compile(rf"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+{_QUALIFIED}\s*\(")
# A REVOKE of EXECUTE from PUBLIC on a no-argument function: it only narrows who may call or attach a function, and
# it is additive only for a function this migration creates, which no older build calls.
_REVOKE_EXECUTE = re.compile(rf"^\s*REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+{_QUALIFIED}\s*\(\s*\)\s+FROM\s+PUBLIC\s*;?\s*$")
# A trigger's events, between its timing and ON: `TRUNCATE` there names an event, not a statement that truncates.
_TRIGGER_EVENTS = re.compile(
    r"\b(BEFORE|AFTER|INSTEAD\s+OF)\s+(?:INSERT|DELETE|TRUNCATE|UPDATE(?:\s+OF\s+\w+(?:\s*,\s*\w+)*)?)"
    r"(?:\s+OR\s+(?:INSERT|DELETE|TRUNCATE|UPDATE(?:\s+OF\s+\w+(?:\s*,\s*\w+)*)?))*\s+ON\b"
)
_UNIQUE_INDEX = re.compile(
    rf"^\s*CREATE\s+UNIQUE\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?:{_TABLE}\s+)?ON\s+(?:ONLY\s+)?{_TABLE}\s*\(",
    re.DOTALL,
)
# The statement kinds the check reads, by how each begins. Anything else is not additive until this check reads it.
_KINDS = (
    ("table", re.compile(r"^\s*CREATE\s+TABLE\b")),
    ("unique index", re.compile(r"^\s*CREATE\s+UNIQUE\s+INDEX\b")),
    ("index", re.compile(r"^\s*CREATE\s+INDEX\b")),
    ("alter", re.compile(r"^\s*ALTER\s+TABLE\b")),
    ("insert", re.compile(r"^\s*INSERT\s+INTO\b")),
    ("update", re.compile(r"^\s*UPDATE\b")),
    ("trigger", re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?TRIGGER\b")),
    ("function", re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b")),
    ("revoke", re.compile(r"^\s*REVOKE\b")),
)
# Anywhere in a statement: each removes, renames or rewrites what older code reads and writes, or makes its write fail.
_NEVER_ADDITIVE = (
    ("DROP", re.compile(r"\bDROP\b")),
    ("RENAME", re.compile(r"\bRENAME\b")),
    ("ALTER COLUMN", re.compile(r"\bALTER\s+COLUMN\b")),
    ("DELETE", re.compile(r"\bDELETE\s+FROM\b")),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\b")),
    ("RAISE", re.compile(r"\bRAISE\b")),
)
_UPDATE = re.compile(rf"\bUPDATE\s+(?:OR\s+\w+\s+)?{_TABLE}\s+SET\b")
# What a module may exempt, with `ADDITIVE_EXEMPT = {kind: why}`: a trigger (a BEFORE trigger returning NULL drops an
# older writer's row), a function (a trigger's body on Postgres) and an UPDATE (rewrites rows older code wrote).
# No scanner sees SQL a function body builds at run time: for a trigger or a function, the stated reason and the
# reviewer who reads the body are the real gate; this check only makes sure both are asked for.
EXEMPTABLE = ("trigger", "function", "update")
_ADD_COLUMN = re.compile(
    r"^ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?!(?:CONSTRAINT|PRIMARY|UNIQUE|CHECK|FOREIGN|EXCLUDE)\b)\w+\s+\w+"
)
_CONSTRAINT = re.compile(r"\b(?:PRIMARY\s+KEY|UNIQUE|CHECK|REFERENCES)\b")


def _code(sql: str) -> str:
    """`sql` upper-cased with literals emptied, comments blanked, quoted identifiers unquoted and dollar-quoted bodies
    tokenized in turn and marked, in one pass."""
    def one(m: re.Match[str]) -> str:
        text = m.group(0)
        if m.group("escape") is not None:
            return _ESCAPE
        if text.startswith("'"):
            return "''"
        if m.group("quoted") is not None:
            name = m.group("quoted").replace('""', '"')
            return name if re.fullmatch(r"\w+", name) else _UNREADABLE
        if m.group("body") is not None:
            return f" {_OPEN}{_code(m.group('body'))}{_CLOSE} "
        return " "

    return _TOKEN.sub(one, sql).upper()


def _several(code: str, kind: str | None) -> bool:
    """Whether a tokenized statement holds more than one statement: a `;` outside its dollar-quoted bodies, outside a
    trigger's or function's BEGIN ... END, other than one trailing. Postgres runs every statement in such a string."""
    top = _BODY.sub(" ", code)
    if kind in ("trigger", "function"):
        top = _without_first_block(top)
    return ";" in top.rstrip().removesuffix(";")


def _without_first_block(code: str) -> str:
    """`code` with its first BEGIN ... END body cut out, the END being the one that closes that BEGIN (each BEGIN or
    CASE inside opens one more level, each END closes one). Unchanged when no BEGIN or no closing END is found, so
    the body's own `;` then still count: an unbalanced body fails closed."""
    depth, start = 0, -1
    for m in _BLOCK_WORD.finditer(code):
        if start < 0:
            if m.group(1) == "BEGIN":
                depth, start = 1, m.start()
            continue
        depth += -1 if m.group(1) == "END" else 1
        if depth == 0:
            return f"{code[:start]} {code[m.end():]}"
    return code


def _actions(clauses: str) -> list[str]:
    """An ALTER TABLE's comma-separated actions, splitting only at commas outside parentheses."""
    parts: list[str] = []
    depth, start = 0, 0
    for i, ch in enumerate(clauses):
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(clauses[start:i])
            start = i + 1
    return [p.strip().rstrip(";").strip() for p in (*parts, clauses[start:])]


def _exemption_violations(exempt: Mapping[str, object]) -> list[str]:
    """Why an `ADDITIVE_EXEMPT` mapping is not one this check takes: a kind it does not know, or no reason given."""
    unknown = [f"exempts {k!r}, which is not one of {', '.join(EXEMPTABLE)}" for k in exempt if k not in EXEMPTABLE]
    bare = [f"exempts {k} with no reason" for k, why in exempt.items()
            if k in EXEMPTABLE and not (isinstance(why, str) and why.strip())]
    return unknown + bare


def _alter_violations(i: int, code: str) -> list[str]:
    alter = _ALTER.match(code)
    if alter is None:
        return [f"statement {i} is an ALTER TABLE this check cannot read"]
    table = alter.group(1).lower()
    reasons: list[str] = []
    for action in _actions(alter.group(2)):
        if not _ADD_COLUMN.match(action):
            reasons.append(f"statement {i} alters {table} by more than adding a column, or in a way this check cannot read")
        elif _CONSTRAINT.search(action):
            reasons.append(f"statement {i} adds a column to {table} with a constraint")
        elif re.search(r"\bNOT\s+NULL\b", action) and not re.search(r"\bDEFAULT\b", action):
            reasons.append(f"statement {i} adds a NOT NULL column to {table} with no DEFAULT")
    return reasons


def additive_violations(statements: Sequence[str], exempt: Mapping[str, object]) -> tuple[str, ...]:
    """Pure. Why `statements`, one migration's, are not additive, one reason per offence; () when they only add.
    Additive means code older than the migration still reads and writes the store as before:
    - every statement is a CREATE TABLE, CREATE INDEX, ALTER TABLE, INSERT, UPDATE, CREATE TRIGGER, CREATE
      FUNCTION or REVOKE this check can read; anything else, or a name it cannot read, is a violation (it fails closed);
    - no DROP, RENAME, ALTER COLUMN, DELETE, TRUNCATE or RAISE anywhere, except TRUNCATE as a trigger's event;
    - a REVOKE only takes EXECUTE from PUBLIC on a no-argument function this migration creates;
    - an ALTER TABLE only adds columns, each without a constraint and, when NOT NULL, with a DEFAULT, or an older
      insert that omits it fails; a unique index only on a table this migration creates;
    - a trigger, a function or an UPDATE only when `exempt` (the module's `ADDITIVE_EXEMPT`) names its kind with a
      reason, since each can drop or rewrite a row older code writes.
    String literals and comments are not read; quoted and schema-qualified table names are."""
    codes = [_code(sql) for sql in statements]
    created = {m.group(1) for code in codes if (m := _CREATED.match(code))}
    functions = {m.group(1) for code in codes if (m := _CREATED_FUNCTION.match(code))}
    allowed = {k for k in EXEMPTABLE if isinstance(exempt.get(k), str) and str(exempt.get(k)).strip()}
    reasons = _exemption_violations(exempt)
    for i, code in enumerate(codes, start=1):
        kind = next((k for k, rx in _KINDS if rx.match(code)), None)
        if _ESCAPE in code:
            reasons.append(f"statement {i} has an escape-string literal (E'...'), which additive DDL does not support")
        if _several(code, kind):
            reasons.append(f"statement {i} holds several statements in one string; split them")
        scanned = _TRIGGER_EVENTS.sub(r"\1 ON", code, count=1) if kind == "trigger" else code
        reasons += [f"statement {i} has {word}" for word, rx in _NEVER_ADDITIVE if rx.search(scanned)]
        if _UPDATE.search(code) and "update" not in allowed:
            reasons.append(f"statement {i} has an UPDATE, which ADDITIVE_EXEMPT does not exempt")
        if kind is None:
            reasons.append(f"statement {i} is not a kind of statement this check reads")
        elif kind in ("trigger", "function") and kind not in allowed:
            reasons.append(f"statement {i} creates a {kind}, which ADDITIVE_EXEMPT does not exempt")
        elif kind == "unique index":
            index = _UNIQUE_INDEX.match(code)
            if index is None:
                reasons.append(f"statement {i} is a CREATE UNIQUE INDEX this check cannot read")
            elif index.group(2) not in created:
                reasons.append(f"statement {i} adds a unique index to {index.group(2).lower()}, a table it did not create")
        elif kind == "table" and _CREATED.match(code) is None:
            reasons.append(f"statement {i} is a CREATE TABLE this check cannot read")
        elif kind == "alter":
            reasons += _alter_violations(i, code)
        elif kind == "update" and _UPDATE.search(code) is None:
            reasons.append(f"statement {i} is an UPDATE this check cannot read")
        elif kind == "revoke":
            revoke = _REVOKE_EXECUTE.match(code)
            if revoke is None:
                reasons.append(f"statement {i} is a REVOKE this check cannot read")
            elif revoke.group(1) not in functions:
                name = revoke.group(1).lower()
                reasons.append(f"statement {i} revokes EXECUTE on {name}, a function it did not create")
    return tuple(reasons)


def _exemptions(m: ModuleType) -> Mapping[str, object]:
    """The module's `ADDITIVE_EXEMPT`, {} when it has none; a value that is not a mapping exempts an unknown kind."""
    exempt: object = getattr(m, "ADDITIVE_EXEMPT", {})
    if not isinstance(exempt, Mapping):
        return {"<not a mapping>": ""}
    return {str(k): v for k, v in cast(Mapping[object, object], exempt).items()}


def accepts(current: int, expected: int, newer_descriptions: Sequence[str]) -> bool:
    """Pure. Whether code whose newest migration is `expected` may use a store at `current`. Equal versions are
    accepted and an older store is not. A newer store is accepted only when `newer_descriptions`, the store's
    `schema_version` descriptions above `expected`, are one per newer version and every one carries
    `ADDITIVE_PREFIX`."""
    if current <= expected:
        return current == expected
    return len(newer_descriptions) == current - expected and all(
        d.startswith(ADDITIVE_PREFIX) for d in newer_descriptions
    )


def newer_refusal(expected: int, newer_descriptions: Sequence[str]) -> str:
    """Pure. Why a store newer than `expected` is refused, for a caller that `accepts` said no to: the first row above
    `expected` that is not additive, by number and description, or that the rows above it are not one per version."""
    blocking = next(
        ((expected + 1 + i, d) for i, d in enumerate(newer_descriptions) if not d.startswith(ADDITIVE_PREFIX)), None
    )
    if blocking is None:
        return f"its schema_version rows above {expected} are not one per version, so they cannot be read as additive"
    version, description = blocking
    return f"migration {version:04d} ({description}) is not additive"


def _steps(
    m: ModuleType, dialect: Dialect, record: str, applied_at: str, current: int
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    """The migration's statements then its schema_version row; none when `current` already covers it."""
    description = recorded_description(m.DESCRIPTION, _declares_additive(m))
    return (
        ()
        if current >= m.VERSION
        else (*((sql, ()) for sql in m.statements(dialect)), (record, (m.VERSION, applied_at, description)))
    )


def check_version(conn: Connection, modules: Sequence[ModuleType | str] | None = None) -> tuple[int, int]:
    """(current, expected) versions. Reads only; applies nothing. `modules` defaults to the harness package's."""
    return _current_version(conn), len(_ordered(default_modules() if modules is None else modules))


def newer_descriptions(conn: Connection, expected: int) -> tuple[str, ...]:
    """Edge. The `schema_version` descriptions above `expected`, in version order; a NULL one reads as ''. Reads
    only; applies nothing."""
    if not _has_version_table(conn):
        return ()
    p = conn.dialect.placeholder
    rows = conn.query_all(f"SELECT description FROM schema_version WHERE version > {p} ORDER BY version", (expected,))
    return tuple("" if r[0] is None else str(r[0]) for r in rows)


def pending(conn: Connection, modules: Sequence[ModuleType | str] | None = None) -> list[tuple[int, str]]:
    """(version, description) for every migration newer than the database. Reads only; applies nothing."""
    current = _current_version(conn)
    ordered = _ordered(default_modules() if modules is None else modules)
    return [(m.VERSION, m.DESCRIPTION) for m in ordered if current < m.VERSION]


def _safe_url(url: str) -> str:
    """Scheme and path only: userinfo, host, port and query never reach output. Mirrors store_copy._safe_url."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"{url.partition(':')[0]}://"
    return f"{parts.scheme}://{parts.path}"


def _backup_dir() -> Path:
    """`$COX_STORE_BACKUP_DIR` if set, else a fixed path under the user's home, computed here, never hardcoded."""
    override = os.environ.get(_BACKUP_DIR_ENV)
    return Path(override) if override else Path.home() / ".local" / "state" / "coxswain" / "backups"


def _backup_name(dialect: Dialect, current: int, newest: int, now: str) -> str:
    """store-v<current>-to-v<newest>-<UTC yyyymmddThhmmssZ>.<ext>. The timestamp is `now`, not a fresh clock read."""
    ts = now.replace("-", "").replace(":", "")
    ext = "sqlite" if dialect.name == "sqlite" else "dump"
    return f"store-v{current}-to-v{newest}-{ts}.{ext}"


def _sqlite_backup(conn: Connection, dest: Path) -> None:
    """Back up the live connection, not a fresh reconnect: a `sqlite:///:memory:` store has no file to reopen."""
    target = sqlite3.connect(str(dest))
    try:
        cast(sqlite3.Connection, conn.raw).backup(target)
    finally:
        target.close()


# A backup before a migration may take minutes on a large store, but never forever.
PG_DUMP_TIMEOUT_S = 1800


# A migration waits this long for the tables it alters, then fails and is retried by the next opener. Long enough for
# a busy chair's row writes to drain; short enough that a session left holding a lock surfaces as an error, not a hang.
MIGRATION_LOCK_TIMEOUT_MS = 300_000


def exempt_from_bounds(dialect: Dialect) -> tuple[str, ...]:
    """Pure. The statements that widen the connection's statement and lock bounds (`store_dialect`) for one
    migration's own transaction: a schema change may run longer than a row write, so its statements are unbounded,
    and it may wait for the tables it alters, but only `MIGRATION_LOCK_TIMEOUT_MS`, never forever."""
    if dialect.name != "postgres":
        return ()
    return ("SET LOCAL statement_timeout = 0", f"SET LOCAL lock_timeout = {MIGRATION_LOCK_TIMEOUT_MS}")


def _without_password(query: str) -> tuple[str, str | None]:
    """Pure. `query` with its `password` parameter dropped, and that parameter's decoded value. The other parameters
    stay byte for byte: libpq percent-decodes them itself, so re-encoding one could change what it reads."""
    kept: list[str] = []
    found: str | None = None
    for item in (query.split("&") if query else []):
        key, _, value = item.partition("=")
        if unquote(key) == "password":
            found = unquote(value)
        else:
            kept.append(item)
    return "&".join(kept), found


def pg_dump_command(url: str, dest: Path) -> tuple[list[str], dict[str, str]]:
    """Pure. The `pg_dump -Fc` argv for the store at `url`, and the variables to add to its environment.

    The password, from the URL's userinfo or its `password` parameter, goes only into `PGPASSWORD`: argv is readable
    by every user on the host through `ps` and `/proc/<pid>/cmdline`, a process's environment only by its owner. User,
    host, port, database and the other parameters stay in the argv's `--dbname`. A `+driver` tag (`postgresql+psycopg`,
    `postgres+psycopg`), which libpq does not know, is dropped. The string is put back together by hand, `//` always
    kept: `urlunsplit` drops it when the host is empty, and libpq refuses `postgresql:/store?host=/var/run/...`."""
    parts = urlsplit(url)
    scheme = parts.scheme.partition("+")[0]
    userinfo, at, hostport = parts.netloc.rpartition("@")
    user, colon, encoded = userinfo.partition(":")
    query, query_password = _without_password(parts.query)
    password = unquote(encoded) if colon else query_password
    netloc = f"{user}@{hostport}" if at and user else hostport
    dbname = f"{scheme}://{netloc}{parts.path}" + (f"?{query}" if query else "") + (
        f"#{parts.fragment}" if parts.fragment else ""
    )
    argv = ["pg_dump", "-Fc", f"--dbname={dbname}", "-f", str(dest)]
    return argv, ({"PGPASSWORD": password} if password else {})


def _pg_dump(url: str, dest: Path, safe_url: str) -> None:
    """`pg_dump -Fc` the store at `url` into `dest`, its password in the child's environment, never in its argv
    (`pg_dump_command`). Never puts `url` or the process's own output in a message."""
    if shutil.which("pg_dump") is None:
        raise MigrationError(
            f"pg_dump is not on PATH; cannot back up {safe_url} before migrating. "
            f"Install pg_dump, or set {_WITHOUT_BACKUP}=1 to migrate without a backup."
        )
    argv, extra_env = pg_dump_command(url, dest)
    try:
        result = subprocess.run(
            argv, capture_output=True, check=False, timeout=PG_DUMP_TIMEOUT_S, env={**os.environ, **extra_env},
        )
    except subprocess.TimeoutExpired:
        raise MigrationError(
            f"pg_dump did not finish backing up {safe_url} in {PG_DUMP_TIMEOUT_S} s. "
            f"Set {_WITHOUT_BACKUP}=1 to migrate without a backup."
        ) from None
    if result.returncode != 0:
        raise MigrationError(
            f"pg_dump exited {result.returncode} backing up {safe_url}. "
            f"Set {_WITHOUT_BACKUP}=1 to migrate without a backup."
        )


def should_back_up(current: int, newest: int, has_tables: bool) -> bool:
    """Pure. Whether opening a store at `current` backs it up before migrating to `newest`: only when a migration is
    pending and the store holds something. A fresh store, at version 0 with no table, has nothing to back up."""
    return current < newest and (current > 0 or has_tables)


def _backup_before_migrate(conn: Connection, url: str, current: int, newest: int, now: str) -> None:
    """Write a backup of `url` unless the override skips it. Never calls `migrate`; the caller does that next."""
    if os.environ.get(_WITHOUT_BACKUP) == "1":
        print(f"WARNING: {_WITHOUT_BACKUP}=1 is set; migrating without a backup.", file=sys.stderr)
        return
    safe_url = _safe_url(url)
    directory = _backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / _backup_name(conn.dialect, current, newest, now)
    if conn.dialect.name == "sqlite":
        _sqlite_backup(conn, dest)
    else:
        _pg_dump(url, dest, safe_url)


def _newer_or_refused(conn: Connection, current: int, newest: int) -> int:
    """Edge. `current`, for a store newer than `newest` whose newer migrations are all additive; reads only. Raises
    `MigrationError`, naming the first newer migration that is not additive, otherwise."""
    newer = newer_descriptions(conn, newest)
    if accepts(current, newest, newer):
        return current
    raise MigrationError(
        f"database is at schema version {current}, newest known migration is {newest}; {newer_refusal(newest, newer)}"
    )


def migrate(conn: Connection, applied_at: str, modules: Sequence[ModuleType | str] | None = None) -> int:
    """Apply each migration newer than the database, one transaction apiece. Returns the version read back.

    The version read before the loop only skips work. Each transaction reads it again under
    the write lock, so a migration another opener committed meanwhile is not run twice.

    A database newer than `modules` is left as it is and its version returned when every newer migration it records
    is additive (`accepts`); otherwise it is refused before any write, naming the first one that is not.
    """
    ordered = _ordered(default_modules() if modules is None else modules)
    newest = len(ordered)
    before = _current_version(conn)
    if before > newest:
        return _newer_or_refused(conn, before, newest)
    for m in ordered[before:]:
        broken = additive_violations(m.statements(conn.dialect), _exemptions(m)) if _declares_additive(m) else ()
        if broken:
            raise MigrationError(f"migration {m.VERSION:04d} declares ADDITIVE but is not: {'; '.join(broken)}")
    conn.execute(_CREATE)
    p = conn.dialect.placeholder
    record = f"INSERT INTO schema_version (version, applied_at, description) VALUES ({p}, {p}, {p})"
    for m in ordered[before:]:
        with conn.transaction():
            for sql in exempt_from_bounds(conn.dialect):
                conn.execute(sql)
            for sql in _lock_versions(conn.dialect):
                conn.execute(sql)
            for sql, params in _steps(m, conn.dialect, record, applied_at, _current_version(conn)):
                conn.execute(sql, params)
    reached = _current_version(conn)
    if reached > newest:
        # A newer build migrated the store between this opener's reads: the same rule as a store found newer.
        return _newer_or_refused(conn, reached, newest)
    if reached != newest:
        raise MigrationError(f"database is at schema version {reached} after migrating, expected {newest}")
    return reached


def open_store(url: str, now: str) -> Connection:
    """Connect, back up first when a migration is pending, bring the schema up to date, and return the connection.

    `now` is the applied-at text. When the database is already at the newest known migration, or is fresh (version 0,
    no table: `should_back_up`), neither a backup file nor a subprocess is written or spawned.
    """
    conn = connect(url)
    try:
        current = _current_version(conn)
        newest = len(_ordered(default_modules()))
        if should_back_up(current, newest, holds_tables(conn)):
            _backup_before_migrate(conn, url, current, newest, now)
        migrate(conn, now)
    except BaseException:
        conn.close()
        raise
    return conn


def exact_refusal(role: str, current: int, expected: int) -> str | None:
    """Pure. None when a store at `current` is at exactly this code's `expected` version; else why a whole-schema
    consumer refuses it. The additive rule (`accepts`) is for code that uses the tables it knows: a copy made by this
    code carries only those, so it would silently drop what a newer store's migrations add."""
    if current == expected:
        return None
    if current < expected:
        return f"the {role} store is at schema version {current}, older than the {expected} this code expects"
    span = f"{expected + 1:04d}" if current == expected + 1 else f"{expected + 1:04d}-{current:04d}"
    return (f"the {role} store is at schema version {current}, newer than the {expected} this code knows: a copy by this "
            f"code would drop what migration {span} adds. Run it from a build at schema version {current}.")


def open_store_exact(url: str, now: str, role: str) -> Connection:
    """Edge. `open_store`, then refuse, closing the connection, unless the store is at exactly this code's newest
    migration: for a consumer that must see the whole schema, which `open_store` alone no longer guarantees once it
    accepts a store newer by additive migrations. Raises `NotExactVersion`."""
    conn = open_store(url, now)
    try:
        refusal = exact_refusal(role, _current_version(conn), len(_ordered(default_modules())))
    except BaseException:
        conn.close()
        raise
    if refusal is not None:
        conn.close()
        raise NotExactVersion(refusal)
    return conn


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.store_migrate", description=cast(str, __doc__).split("\n")[0])
    ap.add_argument("--pending", metavar="URL", required=True, help="list migrations newer than this store's version")
    args = ap.parse_args(argv)
    conn = connect(args.pending)
    try:
        current, newest = check_version(conn)
        if current > newest:
            newer = newer_descriptions(conn, newest)
            if not accepts(current, newest, newer):
                print(f"database is at schema version {current}, newest known migration is {newest}; "
                      f"{newer_refusal(newest, newer)}", file=sys.stderr)
                return 2
            print(f"up to date (v{current}, newer than this code's v{newest} by additive migrations only)")
            return 0
        items = pending(conn)
        if not items:
            print(f"up to date (v{current})")
            return 0
        for version, description in items:
            print(f"{version:04d} {description}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
