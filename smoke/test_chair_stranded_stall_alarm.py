"""A stranded approved task raises the chair's stall alarm.

`t1` is approved in the store, its run record was quarantined for cause `code`, and its commit is not on the phase
branch `epic/ship/p1`: `runs_stranded` lists it, and nothing launches or fails for it. `cox chair run --once` runs a
real tick over the store and writes one `stall_alarm` action row with the task as its subject. A second tick finds the
alarm already raised, reading it back from that row, and writes no second one.
"""

import json

import pytest

from smoke import plan

pytest.importorskip("psycopg")

# `t2` waits on a task that does not exist, so the tick plans no launch for it.
SEED = """import json
from harness import store_api
for task, state, needs in (("t1", "approved", []), ("t2", "ready", ["t0"])):
    store_api.upsert_queue_row({url!r}, {{"kind": "task", "initiative": "ship", "task_id": task, "phase": "p1",
                                         "state": state, "needs": needs, "title": task}}, "2026-11-17T00:00:00Z")
print(json.dumps("seeded"))
"""

ALARMS = """import json, psycopg
with psycopg.connect({url!r}) as conn:
    rows = conn.execute("SELECT kind, status, action_json->>'stall', action_json->>'subject' FROM chair_actions "
                        "WHERE kind IN ('stall_alarm', 'stall_clear') ORDER BY ts, kind").fetchall()
print(json.dumps([list(r) for r in rows]))
"""

# The alarm counts from the approved row's `updated_at`; two hours back is past the default thirty minutes. The profile
# reader has no `chair.stall_alarm` block to set a shorter window with.
BACKDATE = """import json, psycopg
from datetime import UTC, datetime, timedelta
with psycopg.connect({url!r}) as conn:
    conn.execute("UPDATE work_items SET updated_at = %s WHERE task_id = 't1'", ((datetime.now(UTC) - timedelta(hours=2)).isoformat(),))
print(json.dumps("backdated"))
"""


def _stage(sandbox, repo) -> None:
    sandbox.git(repo.path, "branch", "epic/ship/p1")  # the phase branch, at main: no commit of the task on it
    sandbox.write("work/ship/initiative.md", plan.work_item_text({"title": "ship", "repo": "~/src/app", "draft": "false"}, "An initiative."))
    sandbox.write("work/ship/p1/t1.md", plan.work_item_text({"id": "t1", "title": "T", "phase": "p1", "state": "approved"}, "Body."))
    # A pending sibling keeps the phase open, so no land is planned for `t1` and it stays stranded.
    sandbox.write("work/ship/p1/t2.md", plan.work_item_text({"id": "t2", "title": "T2", "phase": "p1", "state": "ready"}, "Body."))
    record = {"run": "r1", "task": "t1", "phase": "p1", "branch": "epic/ship/p1--t1", "repo": "~/src/app", "landed": False,
              "review": {"verdict": "approve"}, "arbitration": "arbiter: skipped (both approved)", "cause": "code"}
    sandbox.write("runs/r1/tasks/p1/t1.json", json.dumps(record))
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) == "seeded"
    assert sandbox.python_json(BACKDATE.format(url=sandbox.store_url)) == "backdated"


def test_a_chair_tick_writes_one_stall_alarm_row_naming_the_stranded_task(sandbox, repo):
    _stage(sandbox, repo)
    first = sandbox.cox("chair", "run", "--once", timeout=180)
    assert first.returncode == 0, first.stdout + first.stderr
    rows = sandbox.python_json(ALARMS.format(url=sandbox.store_url))
    assert rows == [["stall_alarm", "done", "stranded:t1", "t1"]], (rows, first.stdout, first.stderr)
    second = sandbox.cox("chair", "run", "--once", timeout=180)
    assert second.returncode == 0, second.stdout + second.stderr
    assert sandbox.python_json(ALARMS.format(url=sandbox.store_url)) == rows
