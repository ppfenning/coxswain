"""C4, end to end: `cox chair worker --kind quarantine|decompose` as its own process, against a real Postgres.

- A phase finishes with a failure: its task is quarantined (that write sends no notification here) and then its
  `phase_queue` row moves to `finished`. The quarantine worker, polling only every ten minutes, is woken by that move
  alone and makes the triage decision (`request_triage`) within seconds, not on the next tick. No `run_exited`
  courier line is sent.
- A new intake is filed: the decompose worker plans its decompose launch within seconds.
- With both kinds listed, `cox chair run --once --dry-run` plans neither, and each worker's own dry run plans what the
  tick no longer does.

Nothing is stubbed. The decompose worker runs as a dry run, so no decompose run is started in the sandbox; its wake,
gather and plan are the live worker's own (`chair_queue.run`).
"""

from __future__ import annotations

import json
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from smoke import plan

psycopg = pytest.importorskip("psycopg")

TICK_INTERVAL_S = 60.0  # `cox chair run`'s default interval (`chair_run.DEFAULT_INTERVAL`)
WAKE_BOUND_S = 2.0  # from the commit to the start of the pass it woke
DECIDED_BOUND_S = 15.0  # from the commit to the decision on record: well under one tick


def _queue(sandbox, kinds: str) -> None:
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + f"chair:\n  queues: [{kinds}]\n", encoding="utf-8")


class Worker:
    """One `cox chair worker --kind K` process; its output lines, each with the wall time it arrived, read on a thread."""

    def __init__(self, sandbox, kind: str, *args: str) -> None:
        self.kind, self.dry_run = kind, "--dry-run" in args
        argv = [str(sandbox.install.cox), "chair", "worker", "--kind", kind, *args]
        self.proc = subprocess.Popen(
            argv, env=sandbox.env, cwd=sandbox.home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        self.lines: list[tuple[float, str]] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append((time.time(), line.rstrip("\n")))

    def wait_for(self, match: Callable[[str], bool], timeout: float) -> tuple[float, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = next(((t, l) for t, l in list(self.lines) if match(l)), None)
            if found is not None:
                return found
            if self.proc.poll() is not None and not any(match(l) for _, l in self.lines):
                break
            time.sleep(0.05)
        raise AssertionError(f"no matching line in {timeout}s: {self.text()}")

    def is_pass(self, line: str) -> bool:
        return line.startswith(f"chair worker {self.kind} 20") or line.startswith(f"chair worker {self.kind} (dry run) 20")

    def text(self) -> str:
        return "\n".join(l for _, l in self.lines)

    def stop(self) -> int:
        # A live worker takes SIGTERM as an interrupt (systemd's stop); a dry run leaves SIGTERM alone, so it gets ^C.
        self.proc.send_signal(signal.SIGINT if self.dry_run else signal.SIGTERM)
        try:
            return self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=30)


def _pass_started(line: str) -> float:
    stamp = next(part for part in line.split(" ") if part.startswith("20") and part.endswith("Z"))
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()


# -- the phase that fails ------------------------------------------------------------------------------------------

def _seed_phase(sandbox) -> None:
    """A phase-model initiative with one ready task, imported into the store, and its phase claimed by a worker."""
    sandbox.write("work/ship/initiative.md", plan.work_item_text({"id": "ship", "title": "Ship", "lane_model": "phase"}, "Ship it."))
    fields = {"state": "ready", "title": "Task t1", "id": "t1", "phase": "p1", "repo": "app"}
    sandbox.write("work/ship/p1/t1.md", plan.work_item_text(fields, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert sandbox.python_json(CLAIM.format(url=sandbox.store_url)) == ["ship", "p1"]


CLAIM = """import json
from harness import store_phases
from harness.store_dialect import connect
conn = connect({url!r})
try:
    store_phases.apply_changes(conn, [{{"op": "insert", "initiative": "ship", "phase": "p1", "repo": "app"}}], "smoke")
    claim = store_phases.claim_next(conn, "lane-1", [], 1)
finally:
    conn.close()
print(json.dumps([claim.initiative, claim.phase] if claim else None))
"""

# The worker's record of the failed attempt, then the task quarantined, with the trigger on work_items off so that
# write wakes nobody: only the phase row's move may wake the triage.
QUARANTINE = """import json
from harness import store_api
from harness.store_dialect import connect
url = {url!r}
conn = connect(url)
try:
    conn.execute("ALTER TABLE work_items DISABLE TRIGGER cox_notify")
finally:
    conn.close()
row = next(r for r in store_api.queue_rows(url, "ship") if r["task_id"] == "t1")
attempt = {{"run": "ship-1", "cause": "code", "reason": "smoke: the phase's checks failed", "ts": "2026-11-20T10:00:00Z"}}
extra = {{**(row.get("extra") or {{}}), "attempts": [attempt]}}
done = store_api.upsert_queue_row(url, {{**row, "state": "quarantined", "extra": extra}}, "2026-11-20T10:00:00Z")
print(json.dumps(type(done).__name__))
"""

FINISH = """import json
from harness import store_phases
from harness.store_dialect import connect
conn = connect({url!r})
try:
    row = store_phases.get_row(conn, "ship", "p1")
    claim = store_phases.Claim("ship", "p1", row["claim_epoch"], row["lease_until"], row)
    print(json.dumps(store_phases.finish(conn, claim, "lane-1", "failed", "smoke: a task failed its checks")))
finally:
    conn.close()
"""


def _actions(url: str, kind: str) -> list[tuple]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute(
            "SELECT epoch, holder, status, ts FROM chair_actions WHERE kind = %s ORDER BY ts", (kind,)
        ).fetchall()


def test_a_failed_phase_is_triaged_within_seconds_of_its_queue_row_moving_not_on_the_next_tick(sandbox):
    _seed_phase(sandbox)
    _queue(sandbox, "quarantine")
    worker = Worker(sandbox, "quarantine", "--poll", "600", "--min-gap", "0", "--settle", "0")
    try:
        worker.wait_for(lambda l: "holds chair:quarantine" in l, 120)
        worker.wait_for(lambda l: worker.is_pass(l) and "| start |" in l, 180)
        assert sandbox.python_json(QUARANTINE.format(url=sandbox.store_url)) == "Upserted"
        time.sleep(1.0)  # the quarantine write woke nobody: the next pass is the phase row's
        assert not any(worker.is_pass(l) and "| start |" not in l for _, l in worker.lines), worker.text()
        assert sandbox.python_json(FINISH.format(url=sandbox.store_url)) is True
        finished = time.time()
        decided, line = worker.wait_for(lambda l: worker.is_pass(l) and "request_triage" in l, 60)
    finally:
        assert worker.stop() == 0, worker.text()
    assert "| notified: phase_queue |" in line, line
    woke, took = _pass_started(line) - finished, decided - finished
    print(f"triage: pass started {woke:.2f}s and decided {took:.2f}s after the phase row moved")
    assert woke < WAKE_BOUND_S, (woke, worker.text())
    assert took < DECIDED_BOUND_S < TICK_INTERVAL_S, (took, worker.text())
    assert "1x request_triage done" in line, line
    rows = _actions(sandbox.store_url, "request_triage")
    assert [(r[0], r[2]) for r in rows] == [(1, "done")], rows  # once, under the kind lease's first epoch
    task = sandbox.store_row("ship", "t1")
    assert task["state"] == "ready", task  # sent back to ready, flagged so the next run triages it first


# -- the intake ----------------------------------------------------------------------------------------------------

FILE_INTAKE = """import json
from agent_tools import queue_rows
from harness import store_api
row = queue_rows.parse_item("intake", ("n1.md",), {text!r})
print(json.dumps(type(store_api.upsert_queue_row({url!r}, row, "2026-11-20T10:00:00Z")).__name__))
"""


def _file_intake(sandbox) -> float:
    text = plan.work_item_text({"title": "A new idea"}, "Build the new idea.")
    assert sandbox.python_json(FILE_INTAKE.format(url=sandbox.store_url, text=text)) == "Upserted"
    return time.time()


def test_a_new_intake_is_picked_up_by_the_decompose_worker_within_seconds_of_being_filed(sandbox):
    _queue(sandbox, "decompose")
    worker = Worker(sandbox, "decompose", "--dry-run", "--poll", "600", "--min-gap", "0")
    try:
        worker.wait_for(lambda l: worker.is_pass(l) and "| start |" in l, 180)
        assert "launch_decompose" not in worker.text(), worker.text()
        time.sleep(1.0)
        filed = _file_intake(sandbox)
        picked, line = worker.wait_for(lambda l: worker.is_pass(l) and "launch_decompose" in l, 60)
    finally:
        assert worker.stop() == 0, worker.text()
    assert "| notified: " in line and "work_items" in line, line
    woke, took = _pass_started(line) - filed, picked - filed
    print(f"decompose: pass started {woke:.2f}s and planned {took:.2f}s after the intake was filed")
    assert woke < WAKE_BOUND_S, (woke, worker.text())
    assert took < DECIDED_BOUND_S < TICK_INTERVAL_S, (took, worker.text())
    assert "1x launch_decompose dry_run" in line, line


# -- the tick ------------------------------------------------------------------------------------------------------

def _would(stdout: str) -> str:
    """The `would:` segment of a dry-run tick's status line: the actions it planned."""
    status = stdout.strip().splitlines()[-1]
    return next((part for part in status.split(" | ") if part.startswith("would:")), "")


def test_with_quarantine_and_decompose_queued_the_tick_plans_neither(sandbox):
    _seed_phase(sandbox)
    assert sandbox.python_json(QUARANTINE.format(url=sandbox.store_url)) == "Upserted"
    _file_intake(sandbox)
    before = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)
    assert before.returncode == 0, before.stdout + before.stderr
    assert "request_triage:t1" in _would(before.stdout) and "launch_decompose:" in _would(before.stdout), before.stdout
    _queue(sandbox, "quarantine, decompose")
    after = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)
    assert after.returncode == 0, after.stdout + after.stderr
    assert "request_triage" not in _would(after.stdout) and "launch_decompose" not in _would(after.stdout), after.stdout
    for kind, planned in (("quarantine", "request_triage"), ("decompose", "launch_decompose")):
        dry = sandbox.cox("chair", "worker", "--kind", kind, "--once", "--dry-run", timeout=180)
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert f"1x {planned} dry_run" in dry.stdout, json.dumps([kind, dry.stdout])
