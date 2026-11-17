"""The chair's store readers re-read only what changed (H6).

The installed interpreter builds the chair's own dry-run wiring (`cli._chair_run_deps`) against a real Postgres store and
runs the real `gather_facts` three times, as three ticks of one chair process. The second tick follows no write: no
stamped reader may go to the store, and the database's own scan counters for the tables those readers read must not
move. Then one task row is written, and the third tick must re-read exactly the readers of `work_items`, and nothing else.
The counts come from the chair's reader cache (`chair_tick_cache.ReaderCache`), the same counts its timing line prints.
"""

import json
import time

import pytest

psycopg = pytest.importorskip("psycopg")

TICKS = """import datetime, json, os, socket, time
from pathlib import Path
from agent_tools import cli, route, run_store
from harness import store_api

profile_path = Path.home() / ".config" / "agent-tools" / "profile.yaml"
profile = route.parse_profile(profile_path.read_text())
runs_dir = Path(profile["workspace_dir"]) / "runs"
deps = cli._chair_run_deps(runs_dir, profile, "smoke", os.getpid(), socket.gethostname(), True, lambda line: None,
                           profile_path, "store")


def tick():
    before = deps.store_reads()
    deps.beat()
    deps.gather(deps.facts_deps, datetime.datetime.now(datetime.UTC))
    after = deps.store_reads()
    return {{
        "read": sorted(name for name, n in after[0].items() if n > before[0].get(name, 0)),
        "reused": sorted(name for name, n in after[1].items() if n > before[1].get(name, 0)),
    }}


def counters_after(previous):
    # The writer's backend flushes its counts as its connection closes; wait until the store shows the write.
    for _ in range(100):
        now = run_store.table_counters(runs_dir)
        if now != previous:
            return now
        time.sleep(0.1)
    return run_store.table_counters(runs_dir)


def scans():
    import psycopg
    time.sleep(1.5)  # a closed backend's scan counts reach the statistics views within about a second
    with psycopg.connect({url!r}, connect_timeout=10) as conn:
        rows = conn.execute("SELECT relname, seq_scan + COALESCE(idx_scan, 0) FROM pg_stat_user_tables "
                            "WHERE relname = 'work_items'").fetchall()
    return dict(rows)


first = tick()
quiet_before = scans()
second = tick()
quiet_after = scans()
stamp = run_store.table_counters(runs_dir)
written = store_api.set_work_state({url!r}, "ship", "t2", "ready", "smoke", "2026-11-17T00:00:01Z", phase="p1")
counters_after(stamp)
third = tick()
print(json.dumps({{"first": first, "second": second, "third": third, "written": type(written).__name__,
                   "quiet_scans": [quiet_before, quiet_after]}}))
"""

SEED = """import json
from harness import store_api
print(json.dumps(type(store_api.set_work_state({url!r}, "ship", "t1", "ready", "smoke", "2026-11-17T00:00:00Z",
                                              phase="p1")).__name__))
"""

# The readers `cli._chair_run_deps` stamps, by the table each names.
WORK_ITEMS_READERS = ["queue", "work_items"]


def test_a_quiet_tick_reads_nothing_and_one_write_rereads_only_its_readers(sandbox):
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) == "StateSet"
    started = time.monotonic()
    done = sandbox.python_json(TICKS.format(url=sandbox.store_url))
    assert time.monotonic() - started < 60
    assert done["written"] == "StateSet"
    # The first tick reads every stamped reader it reaches; the work-item readers are always among them.
    assert set(WORK_ITEMS_READERS) <= set(done["first"]["read"]), done
    # A quiet tick: every stamped reader reused, none read, and the store saw no scan of the tables they read.
    assert done["second"]["read"] == [], done
    assert set(done["first"]["read"]) <= set(done["second"]["reused"]), done
    before, after = done["quiet_scans"]
    assert after == before, json.dumps(done)
    # One write to work_items: exactly its readers again, the rest reused.
    assert done["third"]["read"] == WORK_ITEMS_READERS, done
    assert set(done["first"]["read"]) - set(WORK_ITEMS_READERS) <= set(done["third"]["reused"]), done
