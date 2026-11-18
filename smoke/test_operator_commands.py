"""`cox stats`, `cox usage` and `cox courier` at their entry points, over what a real run left behind.

The run is a scripted lifecycle run through the harness (`launchable.lifecycle`, as in test_harness_launch.py), so
the stats corpus and the store's run row hold what the harness itself wrote. Spend is written
through the store's own writer (`harness.store_write.Store.record_call`), since a scripted node costs nothing.
"""

from __future__ import annotations

import json

RUN = "life-1"

# One node call costing $12.50, now, written the way the harness writes a call.
SPEND = """
import json
from datetime import UTC, datetime
from harness.store_migrate import open_store
from harness.store_write import Store
now = datetime.now(UTC).isoformat()
store = Store(open_store({url!r}, now))
store.record_call({{"id": "c1", "ts": now, "role": "build", "model": "m", "cost_usd": 12.5, "ok": True}},
                  run_id="spend-1", seq=1)
store.conn.close()
print(json.dumps("recorded"))
"""


def _ran(launchable, repo) -> None:
    done = launchable.lifecycle(repo.path, RUN)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def _ok(sandbox, *args):
    done = sandbox.cox(*args)
    assert done.returncode == 0, f"cox {' '.join(args)}: {done.stdout}{done.stderr}"
    return done


def test_stats_ingest_loads_a_harness_run_and_series_reports_it(launchable, repo):
    sandbox = launchable.sandbox
    _ran(launchable, repo)
    runs, db = sandbox.workspace / "runs", sandbox.home / "stats.db"
    ingested = _ok(sandbox, "stats", "ingest", str(runs), "--db", str(db))
    assert ingested.stdout.startswith(f"1 run(s) ingested from {runs} into {db}"), ingested.stdout
    series = json.loads(_ok(sandbox, "stats", "series", "--db", str(db), "--json").stdout)
    assert [(row["run_id"], row["tasks_landed"], row["quarantined"], row["cost_usd"]) for row in series] == [(RUN, 0, 0, 0)]
    # a second ingest of the same corpus adds no row
    _ok(sandbox, "stats", "ingest", str(runs), "--db", str(db))
    assert len(json.loads(_ok(sandbox, "stats", "series", "--db", str(db), "--json").stdout)) == 1


def test_usage_assess_holds_the_stores_weekly_spend_against_the_ceiling(sandbox):
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + "spend:\n  weekly_ceiling_usd: 10\n", encoding="utf-8")
    runs = str(sandbox.workspace / "runs")
    before = json.loads(_ok(sandbox, "usage", "assess", "--json", "--runs-dir", runs).stdout)
    assert before["verdict"] == "go", before
    assert sandbox.python_json(SPEND.format(url=sandbox.store_url)) == "recorded"
    after = sandbox.cox("usage", "assess", "--runs-dir", runs)
    assert after.returncode == 4, after.stdout + after.stderr
    assert after.stdout.strip() == "stop: weekly spend 125% of weekly ceiling (hard stop at 93%)"


def test_courier_send_inbox_ack_round_trip_through_the_stores_bus(sandbox):
    sandbox.write("work/ship/p1/t1.md", "---\nid: t1\ntitle: T\nstate: ready\nphase: p1\nrepo: app\n---\n\nBody.\n")
    unresolved = sandbox.cox("courier", "send", "coxswain://task/nope", "--to", "chair", "--note", "x")
    assert (unresolved.returncode, unresolved.stdout.strip()) == (2, "courier: coxswain://task/nope does not resolve")
    _ok(sandbox, "courier", "send", "coxswain://task/t1", "--to", "chair", "--note", "look at t1")
    listed = _ok(sandbox, "courier", "inbox", "--label", "chair").stdout.splitlines()
    assert len(listed) == 1 and listed[0].endswith(": cli -> chair: look at t1 (coxswain://task/t1)"), listed
    assert _ok(sandbox, "courier", "inbox", "--label", "someone-else").stdout == ""
    # a workspace with a shared store keeps the bus there, not in courier.jsonl
    assert not (sandbox.workspace / "courier.jsonl").exists()
    _ok(sandbox, "courier", "ack", listed[0].split(":", 1)[0])
    assert _ok(sandbox, "courier", "inbox", "--label", "chair").stdout == ""
    again = sandbox.cox("courier", "ack", "0" * 32)
    assert (again.returncode, again.stdout.strip()) == (2, f"courier: no entry {'0' * 32}")
