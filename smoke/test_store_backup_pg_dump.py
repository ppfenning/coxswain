"""The backup before a migration, on a real Postgres: `open_store` on a store one migration behind runs the real
`pg_dump` into `COX_STORE_BACKUP_DIR`, and on a fresh one (`should_back_up`: version 0, no table) writes nothing.

Both stores are schemas of their own in the test's database, reached through the URL's `search_path`; the installed
interpreter does every step through `harness.store_migrate`. Nothing is stubbed.
"""

import json
import subprocess
from pathlib import Path

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
