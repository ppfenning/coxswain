"""Pure core of the worker's scoped store role: who the role is, what it may touch, and how a probe of it is judged.

One group role with no login, `GROUP_ROLE`, holds every grant. Each worker gets its own login role, `role_name(worker)`,
that is a member of the group and holds nothing of its own, so a machine is removed by dropping one role.

`GRANTS` is the grant table as data. `grant_statements(tables, schema)` turns it into the GRANT and REVOKE statements
for the tables a store has: every present table is first stripped of whatever the group held on it, then given exactly
what `GRANTS` lists, so applying the list again changes nothing and applying a changed list leaves exactly the new
grants. A table `GRANTS` does not name (the land queue, the leases, a table a later migration adds) gets nothing.

Every run also resets both roles themselves: their attributes, every direct grant they hold, and every membership but
the one the plan gives, so a superuser flag, a direct `GRANT DELETE` or a `pg_write_all_data` membership added by hand
is gone after the next run. A password reaches the server only as a SCRAM-SHA-256 verifier computed here
(`scram_verifier`), never as plain text, so a statement log or `pg_stat_statements` never holds it.

No I/O here: `store_scoped` observes the store, applies the statements and runs the probes; the edge supplies the salt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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
READ_ONLY_TABLES = ("work_items", "hosts", "cartridge_snapshots", "landing_records", "schema_version")

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


# The attributes that widen a role, as `ALTER ROLE` spells them. Postgres 16 lets an owner change each one only when
# it holds that attribute itself (a superuser holds them all), even to turn it off; CREATEROLE alone is enough for
# CREATEROLE, as altering a role at all needs it.
WIDENING = ("SUPERUSER", "CREATEDB", "CREATEROLE", "BYPASSRLS", "REPLICATION")


@dataclass(frozen=True)
class RoleAttrs:
    """A role's attributes as `pg_roles` has them. `widening` holds the `WIDENING` names that are on."""

    widening: frozenset[str] = frozenset()
    inherit: bool = True
    login: bool = False
    connection_limit: int = -1
    expires: bool = False


@dataclass(frozen=True)
class RoleState:
    """What the store already has: the group and login roles' attributes (None when a role does not exist) and what
    each is a member of, the `WIDENING` attributes the connecting owner holds, the connection's database and schema,
    and the schema's tables."""

    group: RoleAttrs | None
    login: RoleAttrs | None
    tables: tuple[str, ...]
    schema: str = "public"
    database: str = "postgres"
    group_memberships: tuple[str, ...] = ()
    login_memberships: tuple[str, ...] = ()
    owner: frozenset[str] = frozenset(WIDENING)

    @property
    def group_exists(self) -> bool:
        return self.group is not None

    @property
    def login_exists(self) -> bool:
        return self.login is not None


@dataclass(frozen=True)
class RolePlan:
    """The statements to apply, the password the env file holds after them, and whether they set it on the role.
    The statements carry only the password's SCRAM verifier."""

    statements: tuple[str, ...]
    password: str
    sets_password: bool


SCRAM_ITERATIONS = 4096
SALT_BYTES = 16
_PRINTABLE_ASCII = re.compile(r"[!-~]+")


def scram_verifier(password: str, salt: bytes, iterations: int = SCRAM_ITERATIONS) -> str:
    """Pure. The SCRAM-SHA-256 verifier Postgres stores for `password` (RFC 5802 and 7677, Postgres's own format),
    which `CREATE ROLE ... PASSWORD` accepts as is and never sees the password behind. The password must be printable
    ASCII, where SASLprep changes nothing; ValueError otherwise, rather than a verifier Postgres would not match."""
    if not _PRINTABLE_ASCII.fullmatch(password):
        raise ValueError("a worker password must be printable ASCII with no spaces")
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations)
    stored = hashlib.sha256(hmac.new(salted, b"Client Key", "sha256").digest()).digest()
    server = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored)}:{b64(server)}"


def _quoted(name: str) -> str:
    """A name read back from the catalog, as a quoted identifier."""
    return '"' + name.replace('"', '""') + '"'


def _attribute_changes(attrs: RoleAttrs, login: bool) -> tuple[str, ...]:
    """The `ALTER ROLE` clauses that bring `attrs` to the safe shape: every widening attribute off, INHERIT on (so a
    login uses its group's grants), no connection limit, no expiry, and LOGIN or NOLOGIN. Only what differs: an owner
    that is not a superuser may not even turn off an attribute it lacks, so one already off is never sent."""
    return (
        *(f"NO{name}" for name in WIDENING if name in attrs.widening),
        *(() if attrs.inherit else ("INHERIT",)),
        *(() if attrs.connection_limit == -1 else ("CONNECTION LIMIT -1",)),
        *(() if not attrs.expires else ("VALID UNTIL 'infinity'",)),
        *(() if attrs.login == login else ("LOGIN" if login else "NOLOGIN",)),
    )


def owner_refusal(state: RoleState, login: str) -> str | None:
    """Pure. Why the connecting owner cannot reset the roles, or None: a widening attribute is on for one of them and
    the owner does not hold it, which Postgres requires even to turn it off."""
    stuck = [
        (name, attr)
        for name, attrs in ((GROUP_ROLE, state.group), (login, state.login)) if attrs is not None
        for attr in WIDENING if attr in attrs.widening and attr not in state.owner
    ]
    if not stuck:
        return None
    what = ", ".join(f"{name} has {attr}" for name, attr in stuck)
    fix = "; ".join(f"ALTER ROLE {name} NO{attr}" for name, attr in stuck)
    return (f"{what}, which only a role holding that attribute may clear and this store owner does not; run "
            f"`{fix}` as a superuser, or run this command as one")


def residue(state: RoleState) -> tuple[str, ...]:
    """Pure. What a reset left behind, read back after it: a widening attribute still on, or a membership other than
    the login's in the group. Empty after a reset that worked."""
    roles: tuple[tuple[str, RoleAttrs | None, tuple[str, ...], frozenset[str]], ...] = (
        (GROUP_ROLE, state.group, state.group_memberships, frozenset()),
        ("the login role", state.login, state.login_memberships, frozenset({GROUP_ROLE})),
    )
    return tuple(
        item
        for name, attrs, memberships, allowed in roles
        for item in (
            *(f"{name} still has {a}" for a in sorted(attrs.widening if attrs else ())),
            *(f"{name} is still a member of {m}" for m in sorted(set(memberships) - allowed)),
        )
    )


def _direct_grant_revokes(role: str, schema: str, database: str) -> tuple[str, ...]:
    """Revoke whatever `role` holds directly on the database, the schema and everything in it."""
    return (
        f"REVOKE ALL ON DATABASE {_quoted(database)} FROM {role}",
        f"REVOKE ALL ON SCHEMA {schema} FROM {role}",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role}",
        f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {role}",
    )


def role_plan(state: RoleState, login: str, kept_password: str | None, new_password: str, salt: bytes,
              revoke_public_create: bool = False) -> RolePlan:
    """Pure. What `cox setup store worker-role` applies for `login`, the same on every run.

    The group role is created when absent and the login role when absent. Then both are reset: every widening
    attribute that is on turned off (`_attribute_changes`), every direct grant on the database, the schema and its
    objects revoked, and every membership revoked but the login's in the group. The login gets the
    idle-in-transaction timeout (after a RESET ALL of any other setting), and the group the grant set. The password is
    the one the env file already holds (`kept_password`) when there is one, else `new_password`. It is set, as its
    verifier with `salt`, only when the role is new or the file had none (no one can read a role's password back), so
    a repeat run changes nothing. `revoke_public_create` also takes CREATE on the schema from PUBLIC, which Postgres
    before 15 grants by default. Check `owner_refusal` first: these statements assume the owner can apply them."""
    role, schema = _ident(login), _ident(state.schema)
    password = kept_password or new_password
    sets_password = not (state.login_exists and kept_password)
    secret = (f"PASSWORD {_literal(scram_verifier(password, salt))}",) if sets_password else ()
    group_sql = (
        (f"CREATE ROLE {GROUP_ROLE} NOLOGIN",) if state.group is None
        else tuple(f"ALTER ROLE {GROUP_ROLE} {' '.join(c)}" for c in (_attribute_changes(state.group, False),) if c)
    )
    login_changes = (("LOGIN",) if state.login is None else _attribute_changes(state.login, True)) + secret
    login_sql = (
        (f"CREATE ROLE {role} {' '.join(login_changes)}",) if state.login is None
        else ((f"ALTER ROLE {role} {' '.join(login_changes)}",) if login_changes else ())
    )
    stray_group = sorted(set(state.group_memberships))
    stray_login = sorted(set(state.login_memberships) - {GROUP_ROLE})
    return RolePlan(
        statements=(
            *group_sql,
            *login_sql,
            *(f"REVOKE {_quoted(m)} FROM {GROUP_ROLE}" for m in stray_group),
            *(f"REVOKE {_quoted(m)} FROM {role}" for m in stray_login),
            *_direct_grant_revokes(GROUP_ROLE, schema, state.database),
            *_direct_grant_revokes(role, schema, state.database),
            f"GRANT {GROUP_ROLE} TO {role}",
            f"ALTER ROLE {role} RESET ALL",
            f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = {_literal(IDLE_IN_TRANSACTION_TIMEOUT)}",
            *((f"REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC",) if revoke_public_create else ()),
            *grant_statements(state.tables, state.schema),
        ),
        password=password,
        sets_password=sets_password,
    )


# Taken with pg_advisory_xact_lock for the whole setup transaction, so two setup runs queue rather than fail with
# "tuple concurrently updated" on the shared role rows. Postgres scopes an advisory lock to its database.
SETUP_LOCK_KEY = 0x636F785F776B72  # "cox_wkr"


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


def public_create_advice(schema: str) -> str:
    """What to do when PUBLIC may create in the schema, as Postgres before 15 allows by default: no grant to the
    worker can take that away, so the owner revokes it from PUBLIC."""
    return (f"PUBLIC can create in schema {schema}; run `REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC` as the owner, or "
            "`cox setup store worker-role <name> --revoke-public-create`")


def probe_verdict(outcomes: Sequence[tuple[Probe, str]], public_create_schema: str | None = None) -> tuple[bool, str]:
    """Pure. (ok, detail) for the doctor row from each probe and what happened: `OK`, `REFUSED` (a permission
    error), or another error's one-line text. Ok when every allowed probe ran and every forbidden one was refused.
    `public_create_schema` names the schema when PUBLIC may create in it, and the detail then says how to fix that."""
    wrong = [
        f"{p.label}: {'allowed' if outcome == OK else outcome}"
        for p, outcome in outcomes
        if (outcome != OK if p.allowed else outcome != REFUSED)
    ]
    if not outcomes:
        return False, "no probe ran"
    if wrong:
        advice = [public_create_advice(public_create_schema)] if public_create_schema else []
        return False, "; ".join([*wrong, *advice])
    allowed = sum(1 for p, _ in outcomes if p.allowed)
    return True, f"connects; {allowed} allowed writes work and {len(outcomes) - allowed} forbidden ones are refused"
