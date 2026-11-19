"""The chair keeps its chair_actions window across ticks and tops it up (H6).

A real Postgres store is seeded with a window's worth of rows: 30,000 needs_chair rows of the last two days, old rows
of the kinds the window keeps at any age, and old rows it leaves out. Then:

* the installed `cox chair run --dry-run --as-holder` reads the whole window on its first tick and only tails it on the
  quiet ticks after, which its timing line says (`store: read=chair_actions` on a full read). A row written later with
  a `ts` from two hours before makes the count disagree, so a following tick reads the whole window again, and the tick
  after that is quiet again;
* the chair's own wiring (`cli._chair_run_deps`), run by the installed interpreter, hands the facts exactly the rows a
  plain read of the window returns: a fresh row comes in through the tail alone, without the window read again, and a
  late row makes it read whole. Every intake's decompose runs from the tick's shared reads equal the runs read for
  that intake alone.
"""

import json
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")

SEED = """import json, psycopg
with psycopg.connect({url!r}) as conn:
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute(
        "INSERT INTO chair_actions (ts, epoch, holder, kind, target, status, reason, action_json) "
        "SELECT to_char(%s::timestamptz - make_interval(secs => n * 5), 'YYYY-MM-DD\\"T\\"HH24:MI:SS\\"Z\\"'), 1, 'chair', "
        "'needs_chair', 'i' || mod(n, 300), 'recorded', 'held', "
        "jsonb_build_object('kind', 'needs_chair', 'cause', 'held t' || mod(n, 300), 'initiative', 'i' || mod(n, 300)) "
        "FROM generate_series(1, 30000) AS n",
        ({now!r},),
    )
    conn.execute(
        "INSERT INTO chair_actions (ts, epoch, holder, kind, target, status, reason, action_json) "
        "SELECT '2026-09-0' || (1 + mod(n, 9)) || 'T00:00:' || lpad(mod(n, 60)::text, 2, '0') || 'Z', 1, 'chair', k, 't' || n, "
        "'done', '', jsonb_build_object('kind', k) "
        "FROM generate_series(1, 2000) AS n, (VALUES ('steer_clear'), ('status')) AS kinds(k)"
    )
    conn.execute(
        "INSERT INTO runs (run_id, launched_at, ended_at, status) "
        "SELECT 'idea-' || n, '2026-11-0' || (1 + mod(n, 9)), CASE WHEN mod(n, 3) = 0 THEN NULL ELSE '2026-11-10' END, "
        "CASE WHEN mod(n, 7) = 0 THEN 'refused' ELSE 'done' END FROM generate_series(1, 40) AS n"
    )
    conn.execute("INSERT INTO runs (run_id, launched_at, status) SELECT 'other-' || n, '2026-11-01', 'done' "
                 "FROM generate_series(1, 400) AS n")
    print(json.dumps(conn.execute("SELECT count(*) FROM chair_actions").fetchone()[0]))
"""

WRITE = """import json, psycopg
with psycopg.connect({url!r}) as conn:
    for ts, target in {rows!r}:
        conn.execute(
            "INSERT INTO chair_actions (ts, epoch, holder, kind, target, status, reason, action_json) "
            "VALUES (%s, 1, 'chair', 'needs_chair', %s, 'recorded', '', jsonb_build_object('kind', 'needs_chair'))",
            (ts, target),
        )
print(json.dumps(True))
"""

WIRING = """import datetime, json, os, socket, subprocess, sys
from pathlib import Path
from agent_tools import chair_decompose_runs, chair_read_stale, cli, route

profile_path = Path.home() / ".config" / "agent-tools" / "profile.yaml"
profile = route.parse_profile(profile_path.read_text())
runs_dir = Path(profile["workspace_dir"]) / "runs"
deps = cli._chair_run_deps(runs_dir, profile, "smoke", os.getpid(), socket.gethostname(), True, lambda line: None,
                           profile_path, "store")
window = chair_read_stale.ACTIONS_WINDOW_DAYS


def key_set(rows):
    return sorted((r["ts"], r["holder"], r["kind"], r["target"]) for r in rows)


def tick():
    deps.beat()
    kept = deps.facts_deps.actions()
    plain = chair_read_stale.read_chair_actions(runs_dir, datetime.datetime.now(datetime.UTC), window)
    reads, reuses = deps.store_reads()
    return {{"same": key_set(kept) == key_set(plain), "rows": len(kept),
            "full": reads.get("chair_actions", 0), "tail": reuses.get("chair_actions", 0)}}


first, second = tick(), tick()
fresh = subprocess.run([sys.executable, "-c", {fresh!r}], capture_output=True, text=True)
third = tick()
done = subprocess.run([sys.executable, "-c", {late!r}], capture_output=True, text=True)
fourth = tick()
alone = chair_decompose_runs.read_decompose_runs(runs_dir, "idea")
shared = chair_decompose_runs.read_decompose_runs(runs_dir, "idea", None, chair_decompose_runs.read_tick_runs(runs_dir))
print(json.dumps({{"ticks": [first, second, third, fourth], "write": [fresh.returncode, done.returncode],
                  "decompose_same": [repr(r) for r in alone] == [repr(r) for r in shared], "decompose_runs": len(alone)}}))
"""

TICKS_WANTED = 7
LOOP_TIMEOUT_S = 240


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_note(line: str) -> list[str]:
    """The reader names of a timing line's `store: read=...` part."""
    read = line.split("store: read=", 1)[1].split(" ", 1)[0]
    return [] if read == "none" else read.split(",")


def test_a_quiet_tick_tails_the_window_and_a_late_row_reads_it_whole(sandbox):
    now = datetime.now(UTC)
    assert sandbox.python_json(SEED.format(url=sandbox.store_url, now=_iso(now))) == 34000
    argv = [str(sandbox.install.cox), "chair", "run", "--dry-run", "--as-holder", "--interval", "1", "--max-overruns", "0"]
    loop = subprocess.Popen(argv, env=sandbox.env, cwd=sandbox.home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    timings: list[str] = []
    written_after = None
    deadline = time.monotonic() + LOOP_TIMEOUT_S
    try:
        assert loop.stdout is not None
        for line in loop.stdout:
            if time.monotonic() > deadline:
                break
            if not line.startswith("timing:") or "store: read=" not in line:
                continue
            timings.append(line.strip())
            if len(timings) == 2:
                late = [(_iso(now - timedelta(hours=2)), "late"), (_iso(datetime.now(UTC)), "fresh")]
                assert sandbox.python_json(WRITE.format(url=sandbox.store_url, rows=late)) is True
                written_after = len(timings)
            if len(timings) >= TICKS_WANTED:
                break
    finally:
        loop.kill()  # this test's own child, by its handle
        loop.wait(timeout=30)
    assert len(timings) >= TICKS_WANTED, timings
    reads = [_read_note(t) for t in timings]
    assert "chair_actions" in reads[0], timings
    assert "chair_actions" not in reads[1], timings
    assert written_after == 2
    full_after_write = [n for n in range(2, len(reads)) if "chair_actions" in reads[n]]
    assert full_after_write and full_after_write[0] <= 3, timings  # the tick under way may have read before the write
    assert all("chair_actions" not in r for r in reads[full_after_write[0] + 1 :]), timings


def test_the_chair_hands_the_facts_the_whole_window_and_each_intake_its_own_runs(sandbox):
    now = datetime.now(UTC)
    assert sandbox.python_json(SEED.format(url=sandbox.store_url, now=_iso(now))) == 34000
    fresh = WRITE.format(url=sandbox.store_url, rows=[(_iso(datetime.now(UTC) + timedelta(seconds=1)), "fresh")])
    late = WRITE.format(url=sandbox.store_url, rows=[(_iso(now - timedelta(hours=3)), "late")])
    done = sandbox.python_json(WIRING.format(fresh=fresh, late=late))
    assert done["write"] == [0, 0], done
    ticks = done["ticks"]
    assert all(t["same"] for t in ticks), json.dumps(done)
    # (full reads, tail-only ticks) so far: the first tick reads it whole, the quiet one only tails it
    assert [(t["full"], t["tail"]) for t in ticks[:2]] == [(1, 0), (1, 1)], done
    # a fresh row only: the tail picks it up, and the window is not read again
    assert (ticks[2]["full"], ticks[2]["tail"], ticks[2]["rows"]) == (1, 2, ticks[0]["rows"] + 1), done
    # a row written three hours behind: the count disagrees, so the window is read whole
    assert (ticks[3]["full"], ticks[3]["tail"], ticks[3]["rows"]) == (2, 2, ticks[0]["rows"] + 2), done
    assert done["decompose_same"] and done["decompose_runs"] > 0, done
