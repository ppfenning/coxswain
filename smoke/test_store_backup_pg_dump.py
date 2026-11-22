"""The backup before a migration, on a real Postgres: `open_store` on a store one migration behind runs the real
`pg_dump` into `COX_STORE_BACKUP_DIR`, and on a fresh one (`should_back_up`: version 0, no table) writes nothing.

Both stores are schemas of their own in the test's database, reached through the URL's `search_path`; the installed
interpreter does every step through `harness.store_migrate`. Nothing is stubbed; the last test only puts a script in
front of the real `pg_dump` that records the argv it was handed, to prove the password is not in it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

NOW = "2026-11-20T00:00:00Z"

# argv: the database URL. Creates schema `behind`, migrates it to one short of the newest, then opens it with
# `open_store`; opens the empty schema `fresh` the same way. Prints [newest, behind before, behind after, fresh after].
OPEN_BOTH = """
import json, sys
from harness.store_dialect import connect
from harness.store_migrate import check_version, default_modules, migrate, open_store
url = sys.argv[1]
def at(schema):
    return url + ("&" if "?" in url else "?") + "options=-c%20search_path%3D" + schema
admin = connect(url)
admin.execute("CREATE SCHEMA behind")
admin.execute("CREATE SCHEMA fresh")
admin.close()
behind = connect(at("behind"))
migrate(behind, {now!r}, default_modules()[:-1])
before = check_version(behind)[0]
behind.close()
found = []
for schema in ("behind", "fresh"):
    conn = open_store(at(schema), {now!r})
    found.append(check_version(conn)[0])
    conn.close()
print(json.dumps([len(default_modules()), before, *found]))
"""


def test_open_store_dumps_a_store_one_migration_behind_and_not_a_fresh_one(sandbox, tmp_path: Path):
    backups = tmp_path / "backups"
    env = {**sandbox.env, "COX_STORE_BACKUP_DIR": str(backups)}
    done = subprocess.run([str(sandbox.install.python), "-c", OPEN_BOTH.format(now=NOW), sandbox.store_url],
                          env=env, cwd=sandbox.home, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    newest, before, behind_after, fresh_after = json.loads(done.stdout)
    assert (before, behind_after, fresh_after) == (newest - 1, newest, newest)
    dumps = sorted(backups.iterdir())
    assert [d.name for d in dumps] == [f"store-v{newest - 1}-to-v{newest}-20261120T000000Z.dump"]
    assert dumps[0].read_bytes()[:5] == b"PGDMP"  # pg_dump's custom format, as `pg_restore` reads it


# argv: the database URL. Creates schema `behind` one migration short of the newest, then opens it with `open_store`,
# whose backup runs the first `pg_dump` on PATH.
OPEN_BEHIND = """
import sys
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate, open_store
url = sys.argv[1]
at = url + ("&" if "?" in url else "?") + "options=-c%20search_path%3Dbehind"
admin = connect(url)
admin.execute("CREATE SCHEMA behind")
admin.close()
behind = connect(at)
migrate(behind, {now!r}, default_modules()[:-1])
behind.close()
open_store(at, {now!r}).close()
"""

# Records the argv pg_dump was handed and whether its environment held a password, then runs the real pg_dump on it.
RECORDING_PG_DUMP = """#!/bin/sh
printf '%s\\n' "$@" > "{argv}"
[ -n "$PGPASSWORD" ] && echo set > "{env}"
exec "{real}" "$@"
"""


def test_the_backup_dump_succeeds_with_no_password_in_pg_dumps_argv(sandbox, tmp_path: Path):
    password = urlsplit(sandbox.store_url).password
    assert password, "the smoke server's URL carries a password, so the dump has one to keep out of argv"
    real = shutil.which("pg_dump", path=sandbox.env["PATH"])
    assert real is not None
    shim, argv_file, env_file = tmp_path / "shim", tmp_path / "argv.txt", tmp_path / "env.txt"
    shim.mkdir()
    (shim / "pg_dump").write_text(RECORDING_PG_DUMP.format(argv=argv_file, env=env_file, real=real), encoding="utf-8")
    (shim / "pg_dump").chmod(0o755)
    backups = tmp_path / "backups"
    env = {**sandbox.env, "COX_STORE_BACKUP_DIR": str(backups), "PATH": f"{shim}{os.pathsep}{sandbox.env['PATH']}"}
    done = subprocess.run([str(sandbox.install.python), "-c", OPEN_BEHIND.format(now=NOW), sandbox.store_url],
                          env=env, cwd=sandbox.home, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert argv[0] == "-Fc" and argv[1].startswith("--dbname=postgresql://")
    assert urlsplit(argv[1].removeprefix("--dbname=")).password is None
    # the bare password ("smoke") is also in the database name and the shim's path, so look for it where a URL puts it
    assert not any(f":{password}@" in a or "password=" in a for a in argv)
    assert env_file.read_text(encoding="utf-8").strip() == "set"  # the password reached pg_dump another way
    dumps = list(backups.iterdir())
    assert len(dumps) == 1 and dumps[0].read_bytes()[:5] == b"PGDMP"
