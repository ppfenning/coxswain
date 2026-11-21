"""A killed session's databases and roles are reaped by age, and an old database someone is still connected to is not.
Which names are stale is `plan.stale_databases`, unit-tested in tests/test_smoke_plan.py; this proves the reap that
runs it against a real server, and the one Postgres behaviour it leans on: a DROP without FORCE refuses a database
in use."""

from __future__ import annotations

import hashlib
import subprocess
import time

from smoke import plan
from smoke.conftest import SESSION, SessionDatabases


def _fake_session(label: str) -> str:
    """A session id of this session's own making: another session sharing the server never makes the same one."""
    return hashlib.sha256(f"{SESSION}-{label}".encode()).hexdigest()[:12]


OTHER_SESSION = _fake_session("other")
THIRD_SESSION = _fake_session("third")
THREE_HOURS_S = 3 * 60 * 60

# Holds one connection open until its stdin closes; prints once it is connected.
_HOLD = """import sys, psycopg
with psycopg.connect(sys.argv[1]):
    print("connected", flush=True)
    sys.stdin.read()
"""


def test_reap_drops_an_old_idle_database_and_keeps_a_fresh_one_and_one_in_use(postgres: str, session_databases: SessionDatabases):
    now = int(time.time())
    old = plan.database_name("smoke/test_x.py::test_a", OTHER_SESSION, now - THREE_HOURS_S)
    in_use = plan.database_name("smoke/test_x.py::test_b", OTHER_SESSION, now - THREE_HOURS_S)
    fresh = plan.database_name("smoke/test_x.py::test_a", THIRD_SESSION, now)
    install = session_databases.install
    for name in (old, in_use, fresh):
        session_databases.create(name)
    holder = subprocess.Popen([str(install.python), "-c", _HOLD, plan.with_database(postgres, in_use)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=install.env, cwd=install.root)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "connected"
        dropped = session_databases.reap()
        names = session_databases.names()
        # another session starting now runs the same reap and may get to `old` first, so what is asserted is that it
        # is gone, and that neither reap touched the young one or the one in use
        assert in_use not in dropped and fresh not in dropped
        assert old not in names
        assert in_use in names
        assert fresh in names
    finally:
        holder.communicate(timeout=60)
        session_databases.drop(old, in_use, fresh)


_SQL = """import sys, psycopg
with psycopg.connect(sys.argv[1], autocommit=True) as conn:
    for statement in sys.argv[2:]:
        conn.execute(statement)
"""


def _run_sql(session_databases: SessionDatabases, *statements: str) -> None:
    install = session_databases.install
    done = subprocess.run([str(install.python), "-c", _SQL, session_databases.server, *statements],
                          capture_output=True, text=True, env=install.env, cwd=install.root, timeout=60)
    assert done.returncode == 0, done.stderr


def test_reap_drops_a_killed_session_s_old_roles_and_keeps_a_fresh_one(session_databases: SessionDatabases):
    now = int(time.time())
    old_db = plan.database_name("smoke/test_x.py::test_a", OTHER_SESSION, now - THREE_HOURS_S)
    fresh_db = plan.database_name("smoke/test_x.py::test_a", THIRD_SESSION, now)
    old_owner, fresh_owner = plan.scoped_name(old_db, "owner"), plan.scoped_name(fresh_db, "owner")
    old_login = plan.LOGIN_PREFIX + plan.worker_name(old_db).replace("-", "_")
    roles = (old_owner, old_login, fresh_owner)
    _run_sql(session_databases, *(f'CREATE ROLE "{r}" NOLOGIN' for r in roles))
    try:
        dropped = session_databases.reap()
        names = session_databases.role_names()
        assert old_login not in names and old_owner not in names
        assert fresh_owner in names and fresh_owner not in dropped
    finally:
        session_databases.drop_roles(*plan.role_drop_order(roles))


def test_the_session_end_drops_the_session_s_roles_and_no_other_s(session_databases: SessionDatabases):
    now = int(time.time())
    mine = plan.database_name("smoke/test_x.py::test_a", OTHER_SESSION, now)
    theirs = plan.database_name("smoke/test_x.py::test_a", THIRD_SESSION, now)
    roles = (plan.scoped_name(mine, "owner"), plan.LOGIN_PREFIX + plan.worker_name(mine).replace("-", "_"),
             plan.scoped_name(theirs, "owner"))
    _run_sql(session_databases, *(f'CREATE ROLE "{r}" NOLOGIN' for r in roles))
    session_databases.create(mine)
    try:
        session_databases.drop_session(OTHER_SESSION)
        assert not set(roles[:2]) & set(session_databases.role_names())
        assert mine not in session_databases.names()
        assert roles[2] in session_databases.role_names()
    finally:
        session_databases.drop(mine)
        session_databases.drop_roles(*plan.role_drop_order(roles))
