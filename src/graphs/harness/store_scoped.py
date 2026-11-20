"""Edge of the worker's scoped store role: open the store as the worker, create or reshape the role as the owner, and
probe what the role may do. `store_roles` decides every statement and every verdict; this runs them.

A scoped process never migrates: it has no CREATE right, so `open_store`'s `CREATE TABLE IF NOT EXISTS
schema_version` and its migrations would fail, and a worker on old code must not move the store's schema anyway.
`open_scoped` connects, checks the version with F0's rule (`store_migrate.accepts`: the same version, or a newer one
whose extra migrations are all additive) and writes no DDL.
"""

from __future__ import annotations

from collections.abc import Sequence

from harness import store_roles
from harness.store_dialect import Connection, connect
from harness.store_migrate import accepts, check_version, newer_descriptions, newer_refusal, open_store
from harness.store_read import StoreVersionError
from harness.store_roles import Probe, RolePlan, RoleState

__all__ = ["apply_role", "open_scoped", "probe", "probe_url"]

# Postgres's insufficient_privilege.
_REFUSED_SQLSTATE = "42501"
# The URL schemes `store_dialect.connect` opens as Postgres.
_POSTGRES_SCHEMES = ("postgresql://", "postgres://", "postgresql+psycopg://")


def open_scoped(url: str) -> Connection:
    """Edge. Connect for writing and refuse, closing the connection, a store this code may not use: older than the
    code, or newer by a migration that is not additive. Raises `StoreVersionError`. Applies nothing, ever."""
    conn = connect(url)
    try:
        current, expected = check_version(conn)
        newer = newer_descriptions(conn, expected) if current > expected else ()
    except BaseException:
        conn.close()
        raise
    if not accepts(current, expected, newer):
        conn.close()
        word = "newer" if current > expected else "older"
        why = f"; {newer_refusal(expected, newer)}" if current > expected else ""
        raise StoreVersionError(f"store is at schema version {current}, {word} than the {expected} this code expects"
                                f"{why}; a scoped connection never migrates")
    return conn


def _observe(conn: Connection, login: str) -> RoleState:
    """Edge. Which of the two roles exist, and the tables of the connection's schema."""
    p = conn.dialect.placeholder
    roles = {str(r[0]) for r in conn.query_all(
        f"SELECT rolname FROM pg_roles WHERE rolname IN ({p}, {p})", (store_roles.GROUP_ROLE, login))}
    schema_row = conn.query_one("SELECT current_schema()")
    schema = str(schema_row[0]) if schema_row else "public"
    tables = tuple(str(r[0]) for r in conn.query_all(
        f"SELECT table_name FROM information_schema.tables WHERE table_schema = {p} AND table_type = 'BASE TABLE'",
        (schema,)))
    return RoleState(group_exists=store_roles.GROUP_ROLE in roles, login_exists=login in roles, tables=tables,
                     schema=schema)


def apply_role(owner_url: str, login: str, kept_password: str | None, new_password: str, now: str) -> RolePlan:
    """Edge. As the store's owner: bring the store to this code's schema (`open_store`, the owner's one path), then
    apply `store_roles.role_plan` in one transaction, so a failure leaves the roles and grants as they were. Returns
    the plan; its statements carry the password, so never print them. Raises ValueError for a store that is not
    Postgres, which has no roles, before opening it."""
    if not owner_url.startswith(_POSTGRES_SCHEMES):
        raise ValueError("a scoped worker role needs the Postgres store; this store has no roles")
    conn = open_store(owner_url, now)
    try:
        plan = store_roles.role_plan(_observe(conn, login), login, kept_password, new_password)
        with conn.transaction():
            for sql in plan.statements:
                conn.execute(sql)
        return plan
    finally:
        conn.close()


class _RolledBack(Exception):
    """Raised inside a probe's transaction so it rolls back after a statement that worked."""


def _outcome(exc: Exception) -> str:
    if getattr(exc, "sqlstate", None) == _REFUSED_SQLSTATE:
        return store_roles.REFUSED
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


def probe(conn: Connection, probes: Sequence[Probe] = store_roles.PROBES) -> tuple[tuple[Probe, str], ...]:
    """Edge. Run each probe in a transaction of its own that is always rolled back, and say what happened:
    `store_roles.OK`, `store_roles.REFUSED`, or the error's first line."""

    def one(p: Probe) -> str:
        try:
            with conn.transaction():
                conn.execute(p.sql)
                raise _RolledBack
        except _RolledBack:
            return store_roles.OK
        except Exception as exc:  # every failure is the probe's answer, reported, never raised
            return _outcome(exc)

    return tuple((p, one(p)) for p in probes)


def probe_url(url: str) -> tuple[tuple[Probe, str], ...]:
    """Edge. Open `url` through `open_scoped` and run the probes. Raises what the connection raises."""
    conn = open_scoped(url)
    try:
        return probe(conn)
    finally:
        conn.close()
