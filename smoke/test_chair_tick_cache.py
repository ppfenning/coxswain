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


SCHEMAS = """import json, time
from pathlib import Path
from agent_tools import route, run_store
from harness import store_api

profile = route.parse_profile((Path.home() / ".config" / "agent-tools" / "profile.yaml").read_text())
runs_dir = Path(profile["workspace_dir"]) / "runs"


def work_items_after(previous, wait_s):
    deadline = time.monotonic() + wait_s
    while True:
        now = run_store.table_counters(runs_dir)["work_items"]
        if now != previous or time.monotonic() > deadline:
            return now
        time.sleep(0.1)


# Make public's count the larger, so a reader that kept whichever same-named row came last would follow public's.
for n in range(4):
    store_api.set_work_state({public!r}, "ship", f"p{{n}}", "ready", "smoke", "2026-11-17T00:00:00Z", phase="p1")
time.sleep(2)
start = run_store.table_counters(runs_dir)["work_items"]
public = store_api.set_work_state({public!r}, "ship", "p-only", "ready", "smoke", "2026-11-17T00:00:00Z", phase="p1")
after_public = work_items_after(start, 3)
own = store_api.set_work_state({own!r}, "ship", "t9", "ready", "smoke", "2026-11-17T00:00:01Z", phase="p1")
after_own = work_items_after(after_public, 15)
print(json.dumps([type(public).__name__, type(own).__name__, start, after_public, after_own]))
"""


def test_the_stamp_counts_the_search_path_table_not_a_same_named_one_in_another_schema(sandbox):
    # The store lives in schema `cox`, named by the URL's own options; `public.work_items` exists beside it. Writes to
    # the public one must not move the stamp, and a write to the store's own must (H6 review).
    own = sandbox.store_url + ("&" if "?" in sandbox.store_url else "?") + "options=-c%20search_path%3Dcox"
    with psycopg.connect(sandbox.store_url, connect_timeout=10, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA cox")
    assert sandbox.python_json(SEED.format(url=own)) == "StateSet"  # migrates the `cox` schema and writes t1 there
    provider = sandbox.home / ".config" / "agent-tools" / "provider.yaml"
    provider.write_text(f"storage_url: {own}\n", encoding="utf-8")
    public_kind, own_kind, start, after_public, after_own = sandbox.python_json(
        SCHEMAS.format(public=sandbox.store_url, own=own)
    )
    assert (public_kind, own_kind) == ("StateSet", "StateSet")
    assert after_public == start, "a write to public.work_items moved the cox store's stamp"
    assert after_own > after_public, "a write to cox.work_items did not move the stamp"
