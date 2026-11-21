"""C2, end to end: migration 0021's statement triggers wake a listener on a real Postgres, and never get in a writer's way.

The installed interpreter runs the harness's own store code against a fresh smoke database. A listener
(`harness.store_notify.listen`) hears a commit on a watched table and does not hear a heartbeat. Writers that would
have deadlocked on a shared cursor row (two tables in crossed order, or an unwatched row lock taken first, as
`store_ids.backfill` takes `id_sequence`) all commit, and a session left idle in a transaction blocks no other writer.
Nothing is stubbed.
"""

import pytest

pytest.importorskip("psycopg")

BEATS = """import json
from harness import landing_store, store_hosts, store_notify
from harness.store_dialect import connect

conn = connect({url!r})
store_hosts.host_upsert(conn, "lane-a", "lane-a", 2, "active", "smoke", "2026-11-20T00:00:00Z")
landing_store.enqueue(conn, "phase_merge", "ship", repo="r", now="2026-11-20T00:00:00Z", phase="p1")
ident = landing_store.item_id("phase_merge", "ship", "p1")
landing_store.claim(conn, ident, "lander", now="2026-11-20T00:00:01Z", lease_until="2026-11-20T00:15:00Z")
conn.execute("INSERT INTO workers (name, capabilities_json, lanes, threads, state, version, doctor_json, doctor_at, "
             "started_at, beat_at) VALUES ('lane-a', '[]', 2, 8, 'up', 'v', '{{}}', '', '', '')")
listener = store_notify.listen({url!r})
heard = {{}}
renewed = landing_store.renew(conn, ident, "lander", now="2026-11-20T00:00:03Z", lease_until="2026-11-20T00:30:00Z")
conn.execute("UPDATE workers SET beat_at = '2026-11-20T00:00:04Z' WHERE name = 'lane-a'")
heard["beats"] = sorted(listener.wait(1.5))
store_hosts.host_beat(conn, "lane-a", {{"cox": "v1"}}, "2026-11-20T00:00:05Z")
heard["host beat"] = sorted(listener.wait(5))
finished = landing_store.finish(conn, ident, "lander", now="2026-11-20T00:00:06Z")
heard["finish"] = sorted(listener.wait(5))
conn.execute("UPDATE workers SET state = 'stopping' WHERE name = 'lane-a'")
heard["worker state"] = sorted(listener.wait(5))
listener.close()
conn.close()
print(json.dumps({{"renewed": renewed, "finished": finished, "heard": heard}}))
"""


def test_a_heartbeat_wakes_no_one_and_a_real_change_wakes_the_listener_with_its_table(sandbox):
    done = sandbox.python_json(BEATS.format(url=sandbox.store_url))
    assert (done["renewed"], done["finished"]) == (True, True), done
    # A land claim's renewal (lease_until, updated_at) and a worker's beat (beat_at) send nothing.
    assert done["heard"]["beats"] == [], done
    # A host's beat sets versions_json, which is data (the login check, repos, workspace), so it wakes.
    assert done["heard"]["host beat"] == ["hosts"], done
    assert done["heard"]["finish"] == ["land_queue"], done
    assert done["heard"]["worker state"] == ["workers"], done


WRITERS = """import json, threading, time
import psycopg

url = {url!r}
with psycopg.connect(url, autocommit=True) as c:
    c.execute("INSERT INTO runs (run_id, status) VALUES ('r1', 'running'), ('r2', 'running')")
    c.execute("INSERT INTO id_sequence (scope, next) VALUES ('initiative', 1)")
errors, timings = [], {{}}


def run(name, statements, hold=None):
    started = time.monotonic()
    try:
        with psycopg.connect(url, connect_timeout=10, options="-c lock_timeout=10000") as c:
            for i, sql in enumerate(statements):
                c.execute(sql)
                if i == 0 and hold is not None:
                    hold.set()
                    time.sleep(1.0)  # the other writer runs its first statement now
    except Exception as exc:
        errors.append([name, getattr(exc, "sqlstate", None) or repr(exc)])
    timings[name] = round(time.monotonic() - started, 2)


def pair(first, second):
    hold = threading.Event()
    one = threading.Thread(target=run, args=(first[0], first[1], hold))
    two = threading.Thread(target=run, args=(second[0], second[1]))
    one.start()
    hold.wait(10)
    two.start()
    one.join(30)
    two.join(30)


# Two watched tables, in crossed order, on rows neither writer shares.
pair(("crossed-a", ["UPDATE work_items SET updated_by = 'a' WHERE task_id = 't1'",
                    "UPDATE runs SET status = 'a' WHERE run_id = 'r1'"]),
     ("crossed-b", ["UPDATE runs SET status = 'b' WHERE run_id = 'r2'",
                    "UPDATE work_items SET updated_by = 'b' WHERE task_id = 't2'"]))
# An unwatched row lock first, as store_ids.backfill holds id_sequence, then a watched write; against a watched
# write first, then that same unwatched row. A shared cursor row made this a deadlock (40P01); now the second waits
# for the first's commit and both go through.
pair(("locked-first", ["SELECT next FROM id_sequence WHERE scope = 'initiative' FOR UPDATE",
                       "UPDATE work_items SET updated_by = 'c' WHERE task_id = 't1'"]),
     ("watched-first", ["UPDATE runs SET status = 'c' WHERE run_id = 'r1'",
                        "SELECT next FROM id_sequence WHERE scope = 'initiative' FOR UPDATE"]))

# A session that writes a watched table and then sits idle in its transaction blocks no writer of another row.
idle = psycopg.connect(url, connect_timeout=10)
idle.execute("UPDATE work_items SET updated_by = 'idle' WHERE task_id = 't1'")
run("beside-idle", ["UPDATE work_items SET updated_by = 'e' WHERE task_id = 't2'",
                    "UPDATE runs SET status = 'e' WHERE run_id = 'r2'"])
idle.rollback()
idle.close()
print(json.dumps({{"errors": errors, "timings": timings}}))
"""

SEED = """import json
from harness import store_api
print(json.dumps([type(store_api.set_work_state({url!r}, "ship", t, "ready", "smoke", "2026-11-20T00:00:00Z",
                                                phase="p1")).__name__ for t in ("t1", "t2")]))
"""


def test_writers_in_crossed_order_after_a_held_row_lock_or_beside_an_idle_session_never_deadlock_or_wait(sandbox):
    assert sandbox.python_json(SEED.format(url=sandbox.store_url)) == ["StateSet", "StateSet"]
    done = sandbox.python_json(WRITERS.format(url=sandbox.store_url))
    assert done["errors"] == [], done  # no 40P01 (deadlock) and no 55P03 (lock timeout)
    # The writer beside an idle session does not wait for it.
    assert done["timings"]["beside-idle"] < 5, done
