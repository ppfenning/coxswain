"""Two sessions on one Postgres server do not see each other's databases, and a killed session's are reaped by age."""

from __future__ import annotations

import subprocess
import time

from smoke import plan
from smoke.conftest import Install, SessionDatabases

OTHER_SESSION = "aaaaaaaaaaaa"
THIRD_SESSION = "bbbbbbbbbbbb"
THREE_HOURS_S = 3 * 60 * 60

_PROBE = """import sys, psycopg
with psycopg.connect(sys.argv[1], autocommit=True) as conn:
    if sys.argv[2] == "write":
        conn.execute("CREATE TABLE marker (id int)")
        conn.execute("INSERT INTO marker VALUES (1)")
    else:
        print(conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'marker'").fetchone()[0])
"""


def _probe(install: Install, url: str, mode: str) -> subprocess.CompletedProcess[str]:
    argv = [str(install.python), "-c", _PROBE, url, mode]
    return subprocess.run(argv, env=install.env, cwd=install.root, capture_output=True, text=True, timeout=120, check=True)


def test_two_sessions_on_one_server_do_not_see_each_other(postgres: str, session_databases: SessionDatabases):
    now = int(time.time())
    first = plan.database_name("smoke/test_x.py::test_a", OTHER_SESSION, now)
    second = plan.database_name("smoke/test_x.py::test_a", THIRD_SESSION, now)
    assert first != second
    try:
        session_databases.create(first)
        session_databases.create(second)
        _probe(session_databases.install, plan.with_database(postgres, first), "write")
        assert _probe(session_databases.install, plan.with_database(postgres, first), "count").stdout.strip() == "1"
        assert _probe(session_databases.install, plan.with_database(postgres, second), "count").stdout.strip() == "0"
    finally:
        session_databases.drop(first)
        session_databases.drop(second)


def test_reap_drops_an_old_sessions_database_and_keeps_a_fresh_one(session_databases: SessionDatabases):
    now = int(time.time())
    old = plan.database_name("smoke/test_x.py::test_a", OTHER_SESSION, now - THREE_HOURS_S)
    fresh = plan.database_name("smoke/test_x.py::test_a", THIRD_SESSION, now)
    try:
        session_databases.create(old)
        session_databases.create(fresh)
        assert old in session_databases.reap()
        names = session_databases.names()
        assert old not in names
        assert fresh in names
    finally:
        session_databases.drop(old)
        session_databases.drop(fresh)
