"""`cox chair run --once --dry-run` writes nothing to the store.

A `courier.jsonl` sits in the workspace beside the store's empty courier log. Before the fix, the first courier read of
a tick opened a writable connection, ran `migrate()` (a `CREATE TABLE IF NOT EXISTS`) and loaded that file into the log.

Run twice. Once with the test's database read-only for every new session (`ALTER DATABASE ... SET
default_transaction_read_only = on`), so any write the dry run tries fails on the server: the run must exit 0 and print
no read-only refusal. Once without that guard, so a write would land: the courier log must still be empty. Both times
`schema_version` and every table's row count are as they were before the run.
"""

import json

import pytest

psycopg = pytest.importorskip("psycopg")

TABLES = """SELECT table_name FROM information_schema.tables
WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' ORDER BY table_name"""


def _snapshot(url: str) -> dict[str, object]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        tables = [row[0] for row in conn.execute(TABLES).fetchall()]
        counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        versions = conn.execute("SELECT version, applied_at FROM schema_version ORDER BY version").fetchall()
    return {"counts": counts, "versions": [list(v) for v in versions]}


def _read_only(url: str, on: bool) -> None:
    # This session opts out of the database's setting, so the guard can be lifted again once it is on.
    with psycopg.connect(url, connect_timeout=10, autocommit=True, options="-c default_transaction_read_only=off") as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(f'ALTER DATABASE "{name}" SET default_transaction_read_only = {"on" if on else "off"}')


SEED = """import json
from harness import store_api
print(json.dumps(type(store_api.set_work_state({url!r}, "ship", "t1", "ready", "smoke", "2026-11-17T00:00:00Z",
                                              phase="p1")).__name__))
"""


@pytest.mark.parametrize("guard", [True, False], ids=["read-only store", "writable store"])
def test_a_dry_run_tick_exits_0_and_changes_no_table_row_or_migration(sandbox, guard):
    # A ready task, so the tick plans work and its readers of the queue, attempts and runs have something to read.
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) == "StateSet"
    sandbox.write("courier.jsonl", json.dumps({"id": "m1", "to": "chair", "ref": "coxswain://task/t1"}) + "\n")
    before = _snapshot(sandbox.store_url)
    assert before["counts"]["courier_log"] == 0 and before["versions"], before
    if guard:
        _read_only(sandbox.store_url, True)
    try:
        done = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)
    finally:
        if guard:
            _read_only(sandbox.store_url, False)
    after = _snapshot(sandbox.store_url)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ReadOnlySqlTransaction" not in done.stderr, done.stderr
    assert "read-only transaction" not in done.stdout + done.stderr, done.stdout + done.stderr
    assert after == before, json.dumps({"before": before, "after": after})
