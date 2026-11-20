"""An idle chair with every task held tells the operator once, on the courier.

Two tasks are held for a person in the store (`blocked`, with a `needs you:` hold reason) and nothing else is open, so
no lane runs and no land is in progress. The profile sets `chair: idle_alarm_minutes` to a fraction of a minute.
`cox chair run --once` runs real ticks over the store: the first takes the lease and starts the chair's watch, the
second finds the chair idle past the setting and writes one `stall_alarm` row and one `stall_raised` courier entry that
names each held task, its hold reason and its release command. A third tick finds the spell told and writes neither.
"""

import pytest

pytest.importorskip("psycopg")

SEED = """import json
from harness import store_api
for task, reason in (("t1", "needs you: defect one"), ("t2", "needs you: defect two")):
    store_api.upsert_queue_row({url!r}, {{"kind": "task", "initiative": "ship", "task_id": task, "phase": "p1",
                                         "state": "blocked", "needs": [], "title": task,
                                         "extra": {{"hold_reason": reason}}}}, "2026-11-17T00:00:00Z")
print(json.dumps("seeded"))
"""

ALARMS = """import json, psycopg
with psycopg.connect({url!r}) as conn:
    rows = conn.execute("SELECT kind, status, action_json->>'stall' FROM chair_actions "
                        "WHERE kind IN ('stall_alarm', 'stall_clear') ORDER BY ts, kind").fetchall()
print(json.dumps([list(r) for r in rows]))
"""


# The chair writes the bus through `courier.append_file`, which goes to the store when the profile names one; this reads
# it back the same way, through the installed courier module.
COURIER = """import json
from pathlib import Path
from agent_tools import courier
entries = courier.entries(courier.read_text(Path({path!r})))
print(json.dumps([e for e in entries if e.get("event") in ("stall_raised", "stall_cleared")]))
"""


def _alarms(sandbox) -> list[list[str]]:
    return sandbox.python_json(ALARMS.format(url=sandbox.store_url))


def _courier(sandbox) -> list[dict]:
    return sandbox.python_json(COURIER.format(path=str(sandbox.workspace / "courier.jsonl")))


def test_an_idle_chair_with_every_task_held_writes_one_courier_alarm_naming_each_hold(sandbox):
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + "chair:\n  idle_alarm_minutes: 0.01\n", encoding="utf-8")
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) == "seeded"

    first = sandbox.cox("chair", "run", "--once", timeout=180)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _alarms(sandbox) == [], first.stdout + first.stderr  # the chair has only just started watching

    second = sandbox.cox("chair", "run", "--once", timeout=180)
    assert second.returncode == 0, second.stdout + second.stderr
    rows = _alarms(sandbox)
    assert [(kind, status, stall.partition(":")[0]) for kind, status, stall in rows] == [("stall_alarm", "done", "idle_held")], (
        rows, second.stdout, second.stderr)
    (entry,) = _courier(sandbox)
    assert entry["event"] == "stall_raised" and entry["fields"]["stall"] == rows[0][2]
    note = entry["note"]
    for task, reason in (("t1", "needs you: defect one"), ("t2", "needs you: defect two")):
        assert f"- {task} in ship: {reason} Release: `cox route edit {task} --initiative ship --state ready`" in note, note

    third = sandbox.cox("chair", "run", "--once", timeout=180)
    assert third.returncode == 0, third.stdout + third.stderr
    assert _alarms(sandbox) == rows
    assert len(_courier(sandbox)) == 1
