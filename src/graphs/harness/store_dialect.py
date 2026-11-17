"""The one module that knows which database engine is in use.

Other store modules take a `Connection` from `connect` and ask its dialect for
engine-specific text. Importing this needs only the standard library: the
Postgres driver is imported inside `connect`, on first use.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol, TypeVar, cast
from urllib.parse import parse_qs, urlsplit

from harness.traces_url import redact_url

_T = TypeVar("_T")

__all__ = [
    "POSTGRES",
    "SQLITE",
    "Cell",
    "Connection",
    "Dialect",
    "Json",
    "JsonObject",
    "StoreDriverMissing",
    "connect",
    "default_url",
    "forbidden_constructs",
    "insert_ignore",
    "json_load",
    "json_text",
    "upsert",
]


type Json = bool | int | float | str | list[Json] | dict[str, Json] | None
type JsonObject = dict[str, Json]
# What a driver hands back for one column: text, a number, bytes, or None. Postgres SUM gives Decimal.
type Cell = str | int | float | bytes | Decimal | None


class RawCursor(Protocol):
    """The part of a DB-API cursor `Connection` uses."""

    @property
    def rowcount(self) -> int: ...

    def execute(self, sql: str, params: tuple[object, ...] = (), /) -> object: ...

    def executemany(self, sql: str, rows: list[tuple[object, ...]], /) -> object: ...

    def fetchall(self) -> Sequence[Sequence[Cell]]: ...


class RawConnection(Protocol):
    """The part of a DB-API connection `Connection` uses (sqlite3 and psycopg both satisfy it)."""

    def cursor(self) -> RawCursor: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class StoreDriverMissing(RuntimeError):
    """The Postgres driver is not installed; the `postgres` extra provides it."""


@dataclass(frozen=True)
class Dialect:
    name: str
    placeholder: str
    json_type: str
    bool_type: str  # booleans are stored as 0/1 in this column type on every engine


SQLITE = Dialect(name="sqlite", placeholder="?", json_type="TEXT", bool_type="SMALLINT")
POSTGRES = Dialect(name="postgres", placeholder="%s", json_type="JSONB", bool_type="SMALLINT")

# Token to report, pattern that finds it. Order is the report order.
# REPLACE INTO is sqlite's alias for INSERT OR REPLACE, so it reports under that token.
# strftime and json_extract count only as calls, so a column of that name passes.
_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (token, re.compile(pattern, re.IGNORECASE))
    for token, pattern in (
        ("AUTOINCREMENT", r"\bAUTOINCREMENT\b"),
        ("PRAGMA", r"\bPRAGMA\b"),
        ("INSERT OR REPLACE", r"\bINSERT\s+OR\s+REPLACE\b|\bREPLACE\s+INTO\b"),
        ("strftime", r"\bstrftime\s*\("),
        ("json_extract", r"\bjson_extract\s*\("),
    )
)

# Comments and quoted text: blanked before matching so they neither hide nor fake a token.
_NOT_CODE = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", re.DOTALL)


def forbidden_constructs(sql: str) -> tuple[str, ...]:
    """Banned non-portable tokens found in `sql`, deduplicated, in a fixed order."""
    code = _NOT_CODE.sub(" ", sql)
    return tuple(token for token, rx in _FORBIDDEN if rx.search(code))


def _insert(dialect: Dialect, table: str, columns: Sequence[str]) -> str:
    marks = ", ".join(dialect.placeholder for _ in columns)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"


def insert_ignore(dialect: Dialect, table: str, columns: Sequence[str], key_columns: Sequence[str]) -> str:
    """Insert that skips a row whose key already exists."""
    return f"{_insert(dialect, table, columns)} ON CONFLICT ({', '.join(key_columns)}) DO NOTHING"


def upsert(dialect: Dialect, table: str, columns: Sequence[str], key_columns: Sequence[str]) -> str:
    """Insert that overwrites every non-key column on key conflict; DO NOTHING if all are keys."""
    if not key_columns:
        raise ValueError("upsert needs at least one key column")
    keys = ", ".join(key_columns)
    sets = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in key_columns)
    action = f"DO UPDATE SET {sets}" if sets else "DO NOTHING"
    return f"{_insert(dialect, table, columns)} ON CONFLICT ({keys}) {action}"


def json_text(value: object) -> str:
    """Serialise a value for a json column; keys sorted so equal values give equal text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def json_load(raw: Cell) -> Json:
    """Inverse of `json_text`; None stays None. `raw` is the cell of a json column, text or bytes. `connect` makes both engines return json as text."""
    return None if raw is None else json.loads(cast("str | bytes", raw))


class Connection:
    """Thin wrapper over a DB-API connection that `connect` opened in driver autocommit mode.

    Outside `transaction` each statement stands alone, and a failed one leaves no
    transaction open. `transaction` owns BEGIN, COMMIT and ROLLBACK as plain SQL.
    One lock serialises the threads sharing it (a runner's calls run on a thread pool):
    a statement waits while another thread's transaction is open, and never joins it.
    """

    def __init__(self, raw: RawConnection, dialect: Dialect, begin: str = "BEGIN") -> None:
        self.raw = raw
        self.dialect = dialect
        self._begin = begin
        self._in_tx = False
        self._lock = threading.RLock()

    def _run(self, action: Callable[[RawCursor], _T]) -> _T:
        with self._lock:
            try:
                return action(self.raw.cursor())
            except BaseException:
                if not self._in_tx:
                    self.raw.rollback()
                raise

    def execute(self, sql: str, params: Sequence[object] = ()) -> int:
        """Rows the statement changed; 0 when ON CONFLICT DO NOTHING skipped the row."""

        def act(cur: RawCursor) -> int:
            cur.execute(sql, tuple(params))
            return cur.rowcount

        return self._run(act)

    def executemany(self, sql: str, rows: Sequence[Sequence[object]]) -> int:
        """Rows changed across the batch."""

        def act(cur: RawCursor) -> int:
            cur.executemany(sql, [tuple(r) for r in rows])
            return cur.rowcount

        return self._run(act)

    def query_all(self, sql: str, params: Sequence[object] = ()) -> list[tuple[Cell, ...]]:
        def act(cur: RawCursor) -> list[tuple[Cell, ...]]:
            cur.execute(sql, tuple(params))
            return [tuple(r) for r in cur.fetchall()]

        return self._run(act)

    def query_one(self, sql: str, params: Sequence[object] = ()) -> tuple[Cell, ...] | None:
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Generator[Connection]:
        """Commit on clean exit, roll back and re-raise on error. A nested use in the same thread joins the outer one."""
        with self._lock:
            if self._in_tx:
                yield self
                return
            self.raw.cursor().execute(self._begin)
            self._in_tx = True
            try:
                yield self
                self.raw.cursor().execute("COMMIT")
            except BaseException:
                self.raw.cursor().execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    def close(self) -> None:
        self.raw.close()


def _sqlite_target(url: str) -> str:
    rest = url[len("sqlite://") :]
    if rest in ("", "/:memory:"):
        return ":memory:"
    if rest.startswith("/") and len(rest) > 1:
        return rest[1:]
    raise ValueError(f"unreadable sqlite URL {url!r}: use sqlite:///<path> or sqlite:///:memory:")


def _driver_free_url(url: str) -> str:
    """Drop the SQLAlchemy-style `+psycopg` driver tag: libpq does not know it."""
    prefix = "postgresql+psycopg://"
    return "postgresql://" + url[len(prefix) :] if url.startswith(prefix) else url


# Every bound on a store call, in one place. A store call runs in process (`harness.store_api`), so these are what keep
# a chair tick from hanging on the store: a dead peer fails a connect in seconds, a silent drop surfaces in about a
# minute, a statement runs at most a minute and waits at most half that for a lock. Migrations lift the statement bound
# and widen the lock wait to minutes for their own transactions only (`store_migrate.exempt_from_bounds`, SET LOCAL).
# A URL's own `options` adds to the bounds rather than replacing them (`merged_options`). sqlite, the suites' double, waits
# on a locked file as long as Postgres waits on a lock.
_PG_CONNECT_TIMEOUT_S = 10
_PG_KEEPALIVES_IDLE_S = 30
_PG_KEEPALIVES_INTERVAL_S = 10
_PG_KEEPALIVES_COUNT = 3
STATEMENT_TIMEOUT_MS = 60_000
LOCK_TIMEOUT_MS = 30_000
TCP_USER_TIMEOUT_MS = 60_000
SQLITE_BUSY_TIMEOUT_MS = LOCK_TIMEOUT_MS


BOUND_OPTIONS = f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}"


def merged_options(bounds: str, in_url: Sequence[str]) -> str:
    """Pure. The libpq `options` for a URL whose query carries its own: the bounds first, then the URL's values in
    order. Postgres applies `-c` settings left to right, so a URL that sets `statement_timeout` itself still wins for
    that setting, and one that sets only `search_path` keeps every bound."""
    return " ".join([bounds, *(value.strip() for value in in_url if value.strip())])


def _pg_connect_kwargs(pg_url: str, extra: Mapping[str, str | int | bool] | None = None) -> dict[str, str | int | bool]:
    """Keyword arguments for psycopg.connect. A key in `extra` or in the URL query beats the default, except `options`,
    which merges with the bounds (`merged_options`): a URL setting its own options must not drop the lock bound."""
    in_url = parse_qs(urlsplit(pg_url).query)
    defaults = {
        "connect_timeout": _PG_CONNECT_TIMEOUT_S,
        "keepalives": 1,
        "keepalives_idle": _PG_KEEPALIVES_IDLE_S,
        "keepalives_interval": _PG_KEEPALIVES_INTERVAL_S,
        "keepalives_count": _PG_KEEPALIVES_COUNT,
        "tcp_user_timeout": TCP_USER_TIMEOUT_MS,
        "options": merged_options(BOUND_OPTIONS, in_url.get("options", [])),
        "autocommit": True,
    }
    return {**{k: v for k, v in defaults.items() if k not in in_url or k == "options"}, **(extra or {})}


# The one store is Postgres. sqlite stays in this module only as the test suites' stand-in for it, and opens only
# when the environment says so. No workspace sets this: the suites' conftest files do.
STORE_DOUBLE_ENV = "COXSWAIN_STORE_DOUBLE"
SQLITE_RETIRED = (
    "sqlite is not a supported store: set storage_url in the provider profile to a postgresql:// URL "
    "(cox setup store --store shared)"
)


def double_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """Whether a sqlite store may open: only as the test double."""
    return (os.environ if environ is None else environ).get(STORE_DOUBLE_ENV) == "sqlite"


def connect(url: str) -> Connection:
    """Open a connection for a postgresql: URL (postgresql+psycopg:// too). A sqlite: URL opens only as the test
    double (`double_allowed`); anywhere else it raises ValueError, the same error an unknown scheme gives."""
    pg_url = _driver_free_url(url)
    if url.startswith("sqlite://"):
        if not double_allowed():
            raise ValueError(SQLITE_RETIRED)
        # isolation_level=None: no implicit BEGIN, so a failed statement holds no lock.
        # check_same_thread=False: the Connection's own lock serialises the threads that share it.
        raw = sqlite3.connect(
            _sqlite_target(url), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False
        )
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        # IMMEDIATE takes the write lock at BEGIN, where the busy timeout applies,
        # instead of a late read-to-write upgrade that fails SQLITE_BUSY at once.
        return Connection(cast(RawConnection, raw), SQLITE, begin="BEGIN IMMEDIATE")
    if pg_url.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg
            from psycopg.rows import TupleRow
            from psycopg.types.string import TextLoader
        except ImportError as exc:
            raise StoreDriverMissing(
                "a postgresql URL needs the psycopg driver: install the postgres extra"
            ) from exc
        # psycopg.connect types each keyword on its own; the options dict is built from a URL, so it goes in as one.
        open_pg = cast("Callable[..., psycopg.Connection[TupleRow]]", psycopg.connect)
        raw = open_pg(pg_url, **_pg_connect_kwargs(pg_url))
        # Hand json columns back as text, as sqlite does, so json_load sees one shape.
        raw.adapters.register_loader("json", TextLoader)
        raw.adapters.register_loader("jsonb", TextLoader)
        return Connection(cast(RawConnection, raw), POSTGRES, begin="BEGIN")
    raise ValueError(
        f"unsupported store URL {redact_url(url)!r}: the accepted scheme is postgresql://"
    )


def default_url(runs_dir: str | Path) -> str:
    """sqlite URL for cox.db inside `runs_dir`. The directory is not created."""
    return f"sqlite:///{Path(runs_dir) / 'cox.db'}"
