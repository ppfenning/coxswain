"""F0, end to end: this build opens, reads and writes a store that a newer build migrated past it, when the newer
build's extra migration is additive, and refuses it as before when it is not.

The newer build is this install's own interpreter carrying one more `store_ddl_NNNN` module, numbered one past the
newest this tree has, on its `PYTHONPATH`. It migrates the test's Postgres store through `migrate`'s `modules`
argument, as `open_store` would on that build. The extra migration adds a table and a column, as 0020 and 0021 will.
This build then reaches the store only through its own entry points: `harness.store_migrate --pending`, the store
CLI's `set-state` and `lease acquire` (which open the store writable, as a lane's heartbeat does through `open_store`),
and `store_api.work_item_rows` (which opens it read-only, through `store_read.connect_readonly`). Nothing is stubbed.
"""

import json
import subprocess
from pathlib import Path

NEWEST_PROBE = "import json; from harness.store_migrate import default_modules; print(json.dumps(len(default_modules())))"

# The newer build: migrate the store at argv[1] with this tree's migrations and the extra module named by argv[2],
# then print the store's schema_version rows.
NEWER_BUILD = """
import json, sys
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate
conn = connect(sys.argv[1])
try:
    migrate(conn, "2026-11-20T00:00:00Z", (*default_modules(), sys.argv[2]))
    rows = conn.query_all("SELECT version, description FROM schema_version ORDER BY version")
    print(json.dumps([[int(v), d] for v, d in rows]))
finally:
    conn.close()
"""

READ_PROBE = """
import json, sys
from harness import store_api
rows = store_api.work_item_rows(sys.argv[1], "ship")
print(json.dumps(rows if isinstance(rows, list) else {"refused": str(rows)}, default=str))
"""


def _module_text(version: int, additive: bool) -> str:
    """The source of one extra migration: a new table and a new column with a default, as an additive one would add.
    A non-additive one is the same DDL without the declaration."""
    flag = "ADDITIVE = True\n" if additive else ""
    return (
        f"VERSION = {version}\n"
        'DESCRIPTION = "smoke newer build: notes table and work_items.smoke_note"\n'
        f"{flag}"
        "\n"
        "def statements(dialect):\n"
        "    return (\n"
        '        "CREATE TABLE IF NOT EXISTS smoke_newer_notes (key TEXT NOT NULL, PRIMARY KEY (key))",\n'
        "        \"ALTER TABLE work_items ADD COLUMN smoke_note TEXT NOT NULL DEFAULT ''\",\n"
        "    )\n"
    )


def _newer_build(sandbox, tmp_path: Path, additive: bool) -> tuple[int, list]:
    """Migrate the sandbox store as a build one additive (or non-additive) migration newer would. Returns this build's
    newest version and the store's schema_version rows after."""
    newest = sandbox.python_json(NEWEST_PROBE)
    package = tmp_path / "newer-build" / "smoke_newer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    module = f"store_ddl_{newest + 1:04d}"
    (package / f"{module}.py").write_text(_module_text(newest + 1, additive), encoding="utf-8")
    env = {**sandbox.env, "PYTHONPATH": str(package.parent)}
    done = subprocess.run([str(sandbox.install.python), "-c", NEWER_BUILD, sandbox.store_url, f"smoke_newer.{module}"],
                          env=env, cwd=sandbox.home, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return newest, json.loads(done.stdout)


def _set_state(sandbox):
    return sandbox.harness("harness.store_cli", "--store-url", sandbox.store_url, "set-state", "ship", "t1", "ready",
                           "--by", "smoke", "--phase", "p1")


def _lease(sandbox):
    return sandbox.harness("harness.store_cli", "--store-url", sandbox.store_url, "lease", "acquire", "lane-host-lease",
                           "lane-host", "--ttl", "60")


def _read(sandbox):
    done = subprocess.run([str(sandbox.install.python), "-c", READ_PROBE, sandbox.store_url], env=sandbox.env,
                          cwd=sandbox.home, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_this_build_opens_reads_and_writes_a_store_a_newer_build_migrated_additively(sandbox, tmp_path):
    newest, rows = _newer_build(sandbox, tmp_path, additive=True)
    assert rows[-1] == [newest + 1, "additive: smoke newer build: notes table and work_items.smoke_note"]
    assert len(rows) == newest + 1

    pending = sandbox.harness("harness.store_migrate", "--pending", sandbox.store_url)
    assert pending.returncode == 0, pending.stderr
    assert pending.stdout.strip() == f"up to date (v{newest + 1}, newer than this code's v{newest} by additive migrations only)"

    written = _set_state(sandbox)
    assert written.returncode == 0, written.stderr
    again = _set_state(sandbox)  # the same write repeated changes nothing and is not refused
    assert again.returncode == 0, again.stderr
    leased = _lease(sandbox)
    assert leased.returncode == 0, leased.stderr

    read = _read(sandbox)
    assert [(r["task_id"], r["state"]) for r in read] == [("t1", "ready")], read
    # This build left the newer build's migration as it found it: still the newest row, still additive.
    _, after = _newer_build(sandbox, tmp_path / "again", additive=True)
    assert after == rows


def test_this_build_refuses_a_store_a_newer_build_migrated_with_a_non_additive_migration(sandbox, tmp_path):
    newest, rows = _newer_build(sandbox, tmp_path, additive=False)
    assert rows[-1] == [newest + 1, "smoke newer build: notes table and work_items.smoke_note"]
    blocking = f"migration {newest + 1:04d} (smoke newer build: notes table and work_items.smoke_note) is not additive"

    pending = sandbox.harness("harness.store_migrate", "--pending", sandbox.store_url)
    assert pending.returncode == 2
    assert f"database is at schema version {newest + 1}, newest known migration is {newest}; {blocking}" in pending.stderr

    written = _set_state(sandbox)
    assert written.returncode != 0
    assert blocking in written.stdout + written.stderr
    leased = _lease(sandbox)
    assert leased.returncode != 0
    assert blocking in leased.stdout + leased.stderr

    read = _read(sandbox)
    assert "refused" in read, read
    assert f"newer than the {newest} this code expects; {blocking}" in read["refused"]
