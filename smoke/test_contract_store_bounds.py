"""Contract: a store call `cox` makes in process ends within the store's bounds (H1).

The store CLI ran as a child with a 60 s timeout; in process the bound is the connection's own (`store_dialect`:
`lock_timeout`, `statement_timeout`, `tcp_user_timeout`). Here a second session holds a row lock on a real Postgres
store and the installed interpreter calls `harness.store_api.set_work_state` on that row: it must come back as
`Failed` naming the lock, within the lock bound plus a margin, and leave the row as it was.
"""

import time

import pytest

psycopg = pytest.importorskip("psycopg")

SEED = """import json
from harness import store_api
print(json.dumps(type(store_api.set_work_state({url!r}, "ship", "t1", "ready", "smoke", "2026-11-17T00:00:00Z",
                                              phase="p1")).__name__))
"""
WRITE = """import json, time
from harness import store_api
started = time.monotonic()
found = store_api.set_work_state({url!r}, "ship", "t1", "done", "smoke", "2026-11-17T00:00:01Z", expected="ready")
print(json.dumps([type(found).__name__, getattr(found, "detail", ""), time.monotonic() - started]))
"""
MARGIN_S = 15
# Read through the install, never imported here: this process may carry another checkout's harness.
BOUND = "import json; from harness.store_dialect import LOCK_TIMEOUT_MS; print(json.dumps(LOCK_TIMEOUT_MS))"


def test_a_held_row_lock_makes_a_state_write_fail_within_the_lock_bound(sandbox):
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) == "StateSet"
    with psycopg.connect(sandbox.store_url, connect_timeout=10) as holder:
        holder.execute("SELECT state FROM work_items WHERE initiative = 'ship' AND task_id = 't1' FOR UPDATE")
        started = time.monotonic()
        kind, detail, took = sandbox.python_json(WRITE.format(url=sandbox.store_url))
        waited = time.monotonic() - started
        holder.rollback()
    bound = sandbox.python_json(BOUND) / 1000
    assert kind == "Failed", detail
    assert "LockNotAvailable" in detail or "lock timeout" in detail, detail
    assert bound - 1 <= took <= bound + MARGIN_S and waited <= bound + MARGIN_S + 10
    assert sandbox.store_row("ship", "t1")["state"] == "ready"
