"""C3, end to end: `cox chair worker --kind housekeeping` as its own process, against a real Postgres.

The installed `cox` runs the worker in a sandbox whose profile moves housekeeping off the tick (`chair: queues:`).
A committed write to `work_items` wakes it within a second, and the sweep it plans runs once. `cox chair run --once
--dry-run` then plans no housekeeping. Of two copies only the one holding `chair:housekeeping` acts. With migration
0021's triggers disabled, so no notification is sent at all, the poll still runs it. Nothing is stubbed.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

psycopg = pytest.importorskip("psycopg")

WATCHED = ("work_items", "runs", "phase_queue", "step_results", "review_rounds", "land_queue", "landing_records", "hosts", "workers")


def _queue(sandbox, *, hours: float | None = None, kinds: str = "housekeeping") -> None:
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    block = f"chair:\n  queues: [{kinds}]\n" + (f"  housekeeping_hours: {hours}\n" if hours is not None else "")
    profile.write_text(profile.read_text(encoding="utf-8") + block, encoding="utf-8")


class Worker:
    """One `cox chair worker` process; its output lines, each with the wall time it arrived, are read on a thread."""

    def __init__(self, sandbox, *args: str) -> None:
        argv = [str(sandbox.install.cox), "chair", "worker", "--kind", "housekeeping", *args]
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

    def passes(self) -> list[str]:
        return [l for _, l in self.lines if l.startswith("chair worker housekeeping 20")]

    def text(self) -> str:
        return "\n".join(l for _, l in self.lines)

    def stop(self) -> int:
        self.proc.terminate()
        try:
            return self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=30)


def _housekeeping_rows(url: str) -> list[tuple]:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        return conn.execute("SELECT epoch, holder, status FROM chair_actions WHERE kind = 'housekeeping' ORDER BY ts").fetchall()


SET_STATE = """import json
from harness import store_api
print(json.dumps(type(store_api.set_work_state({url!r}, "ship", {task!r}, "ready", "smoke", "2026-11-20T00:00:00Z",
                                              phase="p1")).__name__))
"""


def _commit_work_item(sandbox, task: str) -> float:
    assert sandbox.python_json(SET_STATE.format(url=sandbox.store_url, task=task)) == "StateSet"
    return time.time()


def _pass_started(line: str) -> float:
    stamp = line.split(" ")[3]
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()


def test_a_committed_write_wakes_the_worker_within_a_second_and_it_runs_housekeeping_once(sandbox):
    _queue(sandbox)
    worker = Worker(sandbox, "--poll", "600", "--min-gap", "0", "--settle", "0")
    try:
        worker.wait_for(lambda l: "holds chair:housekeeping" in l, 120)
        worker.wait_for(lambda l: l.startswith("chair worker housekeeping 20") and "| start |" in l, 180)
        time.sleep(1.0)  # past the first pass's own writes
        committed = _commit_work_item(sandbox, "t-wake")
        _, line = worker.wait_for(lambda l: "notified: work_items" in l, 60)
        woke = _pass_started(line) - committed
        assert woke < 1.0, (woke, worker.text())
    finally:
        assert worker.stop() == 0, worker.text()
    rows = _housekeeping_rows(sandbox.store_url)
    # The first pass found no housekeeping on record and ran the sweep; the woken pass found it done, so planned none.
    assert len(rows) == 1, (rows, worker.text())
    assert rows[0][0] == 1, rows  # recorded under the kind lease's first epoch
    assert "1x housekeeping" in next(l for l in worker.passes() if "| start |" in l), worker.text()
    woken = next(l for l in worker.passes() if "notified: work_items" in l)
    assert "x housekeeping" not in woken, worker.text()


def _would(stdout: str) -> str:
    """The `would:` segment of a dry-run tick's status line: the actions it planned."""
    status = stdout.strip().splitlines()[-1]
    return next((part for part in status.split(" | ") if part.startswith("would:")), "")


def test_with_housekeeping_queued_the_tick_plans_no_housekeeping(sandbox):
    before = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)
    assert before.returncode == 0, before.stdout + before.stderr
    assert "housekeeping" in _would(before.stdout), before.stdout
    _queue(sandbox)
    after = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)
    assert after.returncode == 0, after.stdout + after.stderr
    # No housekeeping action; its `queue_unowned` alarm names the lease (`needs_chair:chair:housekeeping`), not the kind.
    planned = [w.strip() for w in _would(after.stdout).removeprefix("would:").split(",") if w.strip()]
    assert not any(w.startswith("housekeeping:") for w in planned), after.stdout
    # The worker's own dry run plans exactly what the tick no longer does.
    dry = sandbox.cox("chair", "worker", "--kind", "housekeeping", "--once", "--dry-run", timeout=180)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "planned 1 | 1x housekeeping dry_run" in dry.stdout, dry.stdout


def test_of_two_copies_only_the_lease_holder_acts(sandbox):
    _queue(sandbox)
    first = Worker(sandbox, "--poll", "600", "--min-gap", "0", "--settle", "0")
    second = None
    try:
        first.wait_for(lambda l: "holds chair:housekeeping" in l, 120)
        second = Worker(sandbox, "--poll", "600", "--min-gap", "0", "--settle", "0")
        second.wait_for(lambda l: "standby: another worker holds chair:housekeeping" in l, 120)
        first.wait_for(lambda l: "| start |" in l, 180)
        _commit_work_item(sandbox, "t-two")
        first.wait_for(lambda l: "notified: work_items" in l, 60)
        time.sleep(2.0)
    finally:
        codes = [first.stop(), *([second.stop()] if second is not None else [])]
    assert codes == [0, 0], first.text() + "\n---\n" + (second.text() if second else "")
    assert second is not None and second.passes() == [], second.text()
    assert len(_housekeeping_rows(sandbox.store_url)) == 1


def test_with_no_notification_at_all_the_poll_still_runs_housekeeping(sandbox):
    with psycopg.connect(sandbox.store_url, connect_timeout=10, autocommit=True) as conn:
        for table in WATCHED:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER cox_notify")
    # A sweep is due every 2 s (housekeeping_hours as a fraction of an hour), so each poll pass runs one.
    _queue(sandbox, hours=2 / 3600)
    worker = Worker(sandbox, "--poll", "3", "--min-gap", "0", "--settle", "0")
    try:
        worker.wait_for(lambda l: "| start |" in l, 180)
        _commit_work_item(sandbox, "t-silent")
        worker.wait_for(lambda l: l.startswith("chair worker housekeeping 20") and "| poll |" in l and "x housekeeping " in l, 60)
    finally:
        assert worker.stop() == 0, worker.text()
    assert not any("notified" in l for l in worker.passes()), worker.text()
    assert len(_housekeeping_rows(sandbox.store_url)) >= 2, worker.text()
