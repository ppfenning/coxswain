"""A killed session's databases are reaped by age, and an old database someone is still connected to is not.
Which names are stale is `plan.stale_databases`, unit-tested in tests/test_smoke_plan.py; this proves the reap that
runs it against a real server, and the one Postgres behaviour it leans on: a DROP without FORCE refuses a database
in use."""

from __future__ import annotations

import subprocess
import time

from smoke import plan
from smoke.conftest import SessionDatabases

OTHER_SESSION = "aaaaaaaaaaaa"
THIRD_SESSION = "bbbbbbbbbbbb"
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
        assert old in session_databases.reap()
        names = session_databases.names()
        assert old not in names
        assert in_use in names
        assert fresh in names
    finally:
        holder.communicate(timeout=60)
        session_databases.drop(old, in_use, fresh)
