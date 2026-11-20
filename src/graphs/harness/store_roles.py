"""Pure core of the worker's scoped store role: who the role is, what it may touch, and how a probe of it is judged.

One group role with no login, `GROUP_ROLE`, holds every grant. Each worker gets its own login role, `role_name(worker)`,
that is a member of the group and holds nothing of its own, so a machine is removed by dropping one role.

`GRANTS` is the grant table as data. `grant_statements(tables, schema)` turns it into the GRANT and REVOKE statements
for the tables a store has: every present table is first stripped of whatever the group held on it, then given exactly
what `GRANTS` lists, so applying the list again changes nothing and applying a changed list leaves exactly the new
grants. A table `GRANTS` does not name (the land queue, the leases, a table a later migration adds) gets nothing.

No I/O here: `store_scoped` observes the store, applies the statements and runs the probes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from harness import store_ddl_0020

GROUP_ROLE = "cox_worker"
LOGIN_PREFIX = "cox_w_"
IDLE_IN_TRANSACTION_TIMEOUT = "15s"

# The variables the units' environment file carries for a worker: its login role and that role's password. The
# password lives there, mode 600, and never in the provider profile, which is copied into every run's folder.
ROLE_ENV = "COX_WORKER_STORE_ROLE"
PASSWORD_ENV = "COX_WORKER_STORE_PASSWORD"

# Postgres folds an unquoted name to lower case and allows 63 bytes.
_MAX_NAME = 63
_ROLE = re.compile(r"[a-z_][a-z0-9_]*")

# The claim columns of phase_queue: what a worker changes when it claims, beats, and finishes a phase. Not `band`,
# `seq`, `requires_json`, `kind` or `budget_usd`, which are the chair's.
CLAIM_COLUMNS = (
    "state", "outcome", "reason", "claimed_by", "claim_epoch", "run_id", "claimed_at", "heartbeat_at", "lease_until",
    "attempts", "updated_at", "updated_by",
)


@dataclass(frozen=True)
class TableGrant:
    """What the group may do on one table. `update_columns` limits UPDATE to those columns; empty means none."""

    table: str
    privileges: tuple[str, ...]
    update_columns: tuple[str, ...] = ()


_READ = ("SELECT",)
_APPEND = ("SELECT", "INSERT")
_WRITE = ("SELECT", "INSERT", "UPDATE")

# The run's own records, which the harness writes from the lane today: its run, phase and task rows, its attempts,
# its model calls (where a run's cost lives: node_calls.cost_usd, rolled up into phases and runs), its gate
# decisions, its ledger rows and the graph it ran.
RUN_RECORD_TABLES = (
    "runs", "phases", "tasks", "task_records", "attempts", "node_calls", "gate_decisions", "ledger", "graphs",
    "graph_nodes", "graph_edges",
)

# Read so a worker can see what it was asked to do, never written.
READ_ONLY_TABLES = ("work_items", "hosts", "cartridge_snapshots", "landing_records", "schema_version", "store_cursor")

# The grant table. Anything not here gets nothing: land_queue, lander_notices, chair_actions, chair_queue,
# courier_log, leases, id_sequence, and any table no one has decided about yet.
GRANTS: tuple[TableGrant, ...] = (
    TableGrant("phase_queue", ("SELECT",), CLAIM_COLUMNS),
    TableGrant("step_results", _APPEND),
    TableGrant("review_rounds", _APPEND),
    TableGrant("workers", _WRITE),
    *(TableGrant(t, _WRITE) for t in RUN_RECORD_TABLES),
    *(TableGrant(t, _READ) for t in READ_ONLY_TABLES),
)


def role_name(worker: str) -> str:
    """The login role for `worker`: `cox_w_` and the name in lower case, with every character that is not a letter,
    a digit or `_` as `_`. Raises ValueError for an empty name or one too long for Postgres."""
    body = re.sub(r"[^a-z0-9_]", "_", worker.strip().lower())
    if not body.strip("_"):
        raise ValueError("the worker name is empty")
    name = f"{LOGIN_PREFIX}{body}"
    if len(name) > _MAX_NAME:
        raise ValueError(f"the worker name makes a role name longer than {_MAX_NAME} characters")
    return name


def _ident(name: str) -> str:
    """A name safe to put in a statement unquoted; ValueError otherwise. Every name here comes from this module or
    `role_name`, so a refusal is a bug, never input."""
    if not _ROLE.fullmatch(name) or len(name) > _MAX_NAME:
        raise ValueError(f"not a plain lower-case name: {name!r}")
    return name


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _grants_on(grant: TableGrant) -> tuple[str, ...]:
    table = _ident(grant.table)
    whole = (f"GRANT {', '.join(grant.privileges)} ON TABLE {table} TO {GROUP_ROLE}",) if grant.privileges else ()
    columns = ", ".join(_ident(c) for c in grant.update_columns)
    update = (f"GRANT UPDATE ({columns}) ON TABLE {table} TO {GROUP_ROLE}",) if grant.update_columns else ()
    return (*whole, *update)


def grant_statements(tables: Iterable[str], schema: str = "public") -> tuple[str, ...]:
    """The statements that leave `GROUP_ROLE` holding exactly `GRANTS` on the store's `tables`, in `schema`: usage of
    the schema and never CREATE on it; then, per present table in name order, a REVOKE ALL (which takes its column
    grants too) followed by that table's grants. Repeating them changes nothing; a table not in `GRANTS` is revoked
    and given nothing. Tables `GRANTS` names that the store lacks are skipped."""
    present = sorted({_ident(t) for t in tables})
    by_table = {g.table: g for g in GRANTS}
    return (
        f"GRANT USAGE ON SCHEMA {_ident(schema)} TO {GROUP_ROLE}",
        f"REVOKE CREATE ON SCHEMA {_ident(schema)} FROM {GROUP_ROLE}",
        *(s for t in present for s in (
            f"REVOKE ALL ON TABLE {t} FROM {GROUP_ROLE}",
            *(_grants_on(by_table[t]) if t in by_table else ()),
        )),
    )


@dataclass(frozen=True)
class RoleState:
    """What the store already has: whether the group and the login role exist, and its tables in its schema."""

    group_exists: bool
    login_exists: bool
    tables: tuple[str, ...]
    schema: str = "public"


@dataclass(frozen=True)
class RolePlan:
    """The statements to apply, the password the env file holds after them, and whether they set it on the role."""

    statements: tuple[str, ...]
    password: str
    sets_password: bool


def role_plan(state: RoleState, login: str, kept_password: str | None, new_password: str) -> RolePlan:
    """Pure. What `cox setup store worker-role` applies for `login`. The group role is created when absent, the login
    role when absent, and both are always brought to the same shape: login, member of the group, the
    idle-in-transaction timeout, and the grant set. The password is the one the env file already holds
    (`kept_password`) when there is one, else `new_password`; it is set on the role only when the role is new or the
    file had none (no one can read a role's password back), so a repeat run changes nothing."""
    role = _ident(login)
    password = kept_password or new_password
    sets_password = not (state.login_exists and kept_password)
    create_group = () if state.group_exists else (f"CREATE ROLE {GROUP_ROLE} NOLOGIN",)
    secret = f" PASSWORD {_literal(password)}" if sets_password else ""
    login_sql = f"ALTER ROLE {role} LOGIN{secret}" if state.login_exists else f"CREATE ROLE {role} LOGIN{secret}"
    return RolePlan(
        statements=(
            *create_group,
            login_sql,
            f"GRANT {GROUP_ROLE} TO {role}",
            f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = {_literal(IDLE_IN_TRANSACTION_TIMEOUT)}",
            *grant_statements(state.tables, state.schema),
        ),
        password=password,
        sets_password=sets_password,
    )


def worker_url(owner_url: str, login: str, password: str) -> str:
    """The store URL with the owner's user and password replaced by the worker's; host, port, database and query
    kept. The result holds a secret: never print it."""
    parts = urlsplit(owner_url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{quote(login, safe='')}:{quote(password, safe='')}@{host}"))


# --- the probe ------------------------------------------------------------------------------------------------------

PROBE_RUN = "cox-doctor-probe"


def _probe_value(kind: str) -> str:
    return "'{}'" if kind == "JSON" else ("0" if kind in {"INTEGER", "BIGINT", "REAL", "SMALLINT"} else "''")


def _probe_insert(table: str) -> str:
    """An INSERT of one row of zeroes and empty strings, with `run_id` set to `PROBE_RUN`, into a 0020 table."""
    columns = next(c for n, c, _ in store_ddl_0020.TABLES if n == table)
    values = (_literal(PROBE_RUN) if name == "run_id" else _probe_value(kind) for name, kind in columns)
    return f"INSERT INTO {table} ({', '.join(n for n, _ in columns)}) VALUES ({', '.join(values)})"


@dataclass(frozen=True)
class Probe:
    """One statement the doctor runs as the worker inside a transaction it rolls back, and whether it must work."""

    label: str
    sql: str
    allowed: bool


# A forbidden statement is still planned, and so privilege-checked, when it would touch no row: `WHERE false` keeps
# an allowed one from changing anything even before the rollback.
PROBES: tuple[Probe, ...] = (
    Probe("insert step_results", _probe_insert("step_results"), True),
    Probe("insert review_rounds", _probe_insert("review_rounds"), True),
    Probe("update phase_queue claim", "UPDATE phase_queue SET heartbeat_at = heartbeat_at WHERE false", True),
    Probe("update phase_queue band", "UPDATE phase_queue SET band = band WHERE false", False),
    Probe("update work_items", "UPDATE work_items SET state = state WHERE false", False),
    Probe("insert hosts", "INSERT INTO hosts (name) SELECT name FROM hosts WHERE false", False),
    Probe("delete land_queue", "DELETE FROM land_queue WHERE false", False),
    Probe("create table", "CREATE TABLE cox_worker_probe (x INTEGER)", False),
)

OK = "ok"
REFUSED = "refused"


def probe_verdict(outcomes: Sequence[tuple[Probe, str]]) -> tuple[bool, str]:
    """Pure. (ok, detail) for the doctor row from each probe and what happened: `OK`, `REFUSED` (a permission
    error), or another error's one-line text. Ok when every allowed probe ran and every forbidden one was refused."""
    wrong = [
        f"{p.label}: {'allowed' if outcome == OK else outcome}"
        for p, outcome in outcomes
        if (outcome != OK if p.allowed else outcome != REFUSED)
    ]
    if not outcomes:
        return False, "no probe ran"
    if wrong:
        return False, "; ".join(wrong)
    allowed = sum(1 for p, _ in outcomes if p.allowed)
    return True, f"connects; {allowed} allowed writes work and {len(outcomes) - allowed} forbidden ones are refused"
