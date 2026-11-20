"""The chair's store readers re-read only what changed (H6), keyed by the store's change cursor (C2).

The installed interpreter builds the chair's own dry-run wiring (`cli._chair_run_deps`) against a real Postgres store and
runs the real `gather_facts` three times, as three ticks of one chair process. The second tick follows no write: no
stamped reader may go to the store, and the database's own scan counters for the tables those readers read must not
move. Then one task row is written, and the very next tick, with no wait, must re-read exactly the readers of
`work_items`, and nothing else: the cursor (`harness.store_ddl_0021`) moved when the write committed. The counts come
from the chair's reader cache (`chair_tick_cache.ReaderCache`), the same counts its timing line prints.

The cursor's own cases follow: a heartbeat moves nothing, concurrent writers each count, and two writers that take two
watched tables in opposite orders both commit, queued on the cursor's gate row instead of deadlocked.
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
moved = run_store.table_counters(runs_dir)  # no wait: the cursor committed with the write
third = tick()
print(json.dumps({{"first": first, "second": second, "third": third, "written": type(written).__name__,
                   "quiet_scans": [quiet_before, quiet_after],
                   "work_items": [stamp["work_items"], moved["work_items"]], "gate": [stamp["*"], moved["*"]]}}))
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
    # The write moved its table's cursor and the gate before the next tick read them.
    assert done["work_items"][1] > done["work_items"][0] and done["gate"][1] > done["gate"][0], done
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


BEATS = """import json
from harness import landing_store, store_hosts
from harness.store_dialect import connect

conn = connect({url!r})


def seqs():
    return dict(conn.query_all("SELECT name, seq FROM store_cursor"))


store_hosts.host_upsert(conn, "lane-a", "lane-a", 2, "active", "smoke", "2026-11-20T00:00:00Z")
landing_store.enqueue(conn, "phase_merge", "ship", repo="r", now="2026-11-20T00:00:00Z", phase="p1")
ident = landing_store.item_id("phase_merge", "ship", "p1")
claimed = landing_store.claim(conn, ident, "lander", now="2026-11-20T00:00:01Z", lease_until="2026-11-20T00:15:00Z")
before = seqs()
beat = store_hosts.host_beat(conn, "lane-a", {{"cox": "v1"}}, "2026-11-20T00:00:02Z")
renewed = landing_store.renew(conn, ident, "lander", now="2026-11-20T00:00:03Z", lease_until="2026-11-20T00:30:00Z")
after_beats = seqs()
finished = landing_store.finish(conn, ident, "lander", now="2026-11-20T00:00:04Z")
after_finish = seqs()
conn.close()
print(json.dumps({{"claimed": claimed, "beat": beat is not None, "renewed": renewed, "finished": finished,
                   "before": before, "after_beats": after_beats, "after_finish": after_finish}}))
"""


def test_a_heartbeat_moves_no_cursor_row_and_the_write_after_it_moves_its_table_and_the_gate(sandbox):
    done = sandbox.python_json(BEATS.format(url=sandbox.store_url))
    assert [done[k] for k in ("claimed", "beat", "renewed", "finished")] == [True, True, True, True], done
    # A host's beat (beat_at, versions_json) and a land claim's renewal (lease_until, updated_at) stamp nothing.
    assert done["after_beats"] == done["before"], done
    moved = {k: done["after_finish"][k] - done["before"][k] for k in done["before"]
             if done["after_finish"][k] != done["before"][k]}
    assert moved == {"*": 1, "land_queue": 1}, done


WRITERS = """import json, threading, time
import psycopg

url = {url!r}


def seqs():
    with psycopg.connect(url, autocommit=True) as c:
        return dict(c.execute("SELECT name, seq FROM store_cursor").fetchall())


with psycopg.connect(url, autocommit=True) as c:
    c.execute("INSERT INTO runs (run_id, status) VALUES ('r1', 'running')")
before = seqs()
errors = []


def run(statements, holding=None):
    try:
        with psycopg.connect(url, connect_timeout=10) as c:  # one transaction, committed as the block exits
            for i, sql in enumerate(statements):
                c.execute(sql)
                if i == 0 and holding is not None:
                    holding.set()
                    time.sleep(1.0)  # the other writer starts now, and blocks inside its first statement
    except Exception as exc:
        errors.append(getattr(exc, "sqlstate", None) or repr(exc))


# Two writers of one table at once: both commit and each counts.
same = [threading.Thread(target=run, args=([f"UPDATE work_items SET updated_by = 'w{{n}}' WHERE task_id = 't1'"],))
        for n in range(2)]
for t in same:
    t.start()
for t in same:
    t.join(30)
after_same = seqs()

# Two writers that take work_items and runs in opposite orders, the second starting while the first holds its first
# table and both writing the same runs row: with the gate taken first, the second waits at its first statement and
# both commit; with the gate taken last, each would hold a row the other needs.
holding = threading.Event()
one = threading.Thread(target=run, args=(["UPDATE work_items SET updated_by = 'a' WHERE task_id = 't1'",
                                          "UPDATE runs SET status = 'a' WHERE run_id = 'r1'"], holding))
two = threading.Thread(target=run, args=(["UPDATE runs SET status = 'b' WHERE run_id = 'r1'",
                                          "UPDATE work_items SET updated_by = 'b' WHERE task_id = 't1'"],))
one.start()
holding.wait(10)
two.start()
one.join(30)
two.join(30)
after_crossed = seqs()
print(json.dumps({{"errors": errors, "before": before, "after_same": after_same, "after_crossed": after_crossed}}))
"""


def test_concurrent_writers_each_count_and_writers_in_opposite_orders_queue_instead_of_deadlocking(sandbox):
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) == "StateSet"
    done = sandbox.python_json(WRITERS.format(url=sandbox.store_url))
    assert done["errors"] == [], done  # no 40P01 (deadlock) and no 55P03 (lock timeout)
    before, same, crossed = done["before"], done["after_same"], done["after_crossed"]
    assert (same["work_items"] - before["work_items"], same["*"] - before["*"]) == (2, 2), done
    assert (crossed["work_items"] - same["work_items"], crossed["runs"] - same["runs"]) == (2, 2), done
    assert crossed["*"] - same["*"] == 4, done
