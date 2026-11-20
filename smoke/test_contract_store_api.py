"""Contract: `cox` <-> the store, in process through `harness.store_api` (H1).

`cox runs pause` and `cox runs resume` once ran `python -m harness.store_cli pause` under the profile's harness python,
and `cox runs move` called it with no store at all, so its pause step always failed. They now call the store API in
the interpreter `cox` runs in. Here the installed `cox` pauses and resumes a run in a real Postgres store, and the run
row is read back; an unknown run is refused, not failed.
"""

import json

SEED = """import json
from harness.store_migrate import open_store
from harness.store_write import Store
conn = open_store({url!r}, "2026-11-17T00:00:00Z")
try:
    Store(conn).record_run({{"run_id": "ship-1", "graph": "epic", "status": "running"}})
finally:
    conn.close()
print(json.dumps(True))
"""
PAUSED = """import json
from harness.store_migrate import open_store
from harness.store_pause import is_paused
conn = open_store({url!r}, "2026-11-17T00:00:00Z")
try:
    print(json.dumps(is_paused(conn, "ship-1")))
finally:
    conn.close()
"""


def _paused(sandbox) -> bool:
    return sandbox.python_json(PAUSED.format(url=sandbox.store_url))


def test_cox_pauses_and_resumes_a_run_in_the_store(sandbox):
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) is True

    paused = sandbox.cox("runs", "pause", "ship-1", "--reason", "smoke", "--json")
    assert paused.returncode == 0, paused.stdout + paused.stderr
    assert json.loads(paused.stdout) == {"outcome": "paused", "run": "ship-1", "reason": "smoke"}
    assert _paused(sandbox) is True

    resumed = sandbox.cox("runs", "resume", "ship-1")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert _paused(sandbox) is False


def test_cox_refuses_to_pause_a_run_the_store_does_not_hold(sandbox):
    refused = sandbox.cox("runs", "pause", "ship-9")
    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert "refused ship-9: not in store" in refused.stdout
