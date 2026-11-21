"""One decompose per intake, against a real Postgres, with the queue workers on (all five kinds listed). Nothing is stubbed.

The incident (2026-11-21): one readied intake, placed by the launch worker on a lane host. A hosted launch returns at
once, and the intake names no initiative until the run writes its rows, so every `runs` notification the run itself
sent woke the launch worker into launching it again: four decomposes in two minutes, three refused by the first's lease.

- In flight elsewhere. The intake is queued and its decompose run `n1-1` is live in the shared store, as a lane host's
  run writes it, with no end. The live launch worker (`--min-gap 1`, so each notification is a pass) takes several
  passes, woken by `runs` writes of that run's row: none launches a decompose. The fixture has no lane host, so the
  hosted run is the row its harness writes; the worker reads only the store.
- Ready one intake. The launch worker decomposes it here, which writes its rows, and then takes several more passes
  woken by `runs` notifications: exactly one `launch_decompose` is recorded, and the initiative carries
  `lane_model: phase` from the team cartridge.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import pytest

from smoke import plan
from smoke.test_chair_launch_queue import _cartridge, _decompose_ready, _launches, _profile, _queue, _until

psycopg = pytest.importorskip("psycopg")

ALL_KINDS = "housekeeping, watch, quarantine, decompose, launch"
PASSES = 4  # passes after the first, each woken by a `runs` write


class Worker:
    """`cox chair worker --kind launch`, live, with each notification a pass of its own."""

    def __init__(self, sandbox) -> None:
        self.log = sandbox.home / "launch-worker.log"
        argv = [str(sandbox.install.cox), "chair", "worker", "--kind", "launch", "--settle", "0", "--poll", "900",
                "--min-gap", "1", "--label", "launch-worker"]
        with open(self.log, "w", encoding="utf-8") as out:
            self.proc = subprocess.Popen(argv, env=sandbox.env, cwd=sandbox.home, stdout=out, stderr=subprocess.STDOUT, text=True)

    def said(self) -> str:
        return self.log.read_text(encoding="utf-8")

    def passes(self) -> list[str]:
        return [line for line in self.said().splitlines() if line.startswith("chair worker launch 20")]

    def stop(self) -> str:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=30)
        return self.said()


def _ready_one_intake(launchable) -> None:
    sandbox = launchable.sandbox
    _decompose_ready(launchable)
    _cartridge(launchable, max_in_flight=2, lane_model="phase")  # a lane is free beside the live run
    _queue(_profile(sandbox), ALL_KINDS)
    sandbox.write("intake/n1.md", plan.work_item_text({"title": "Idea n1", "repo": "app"}, "Build n1."))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sql(url: str, statement: str, *args: object) -> None:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        conn.execute(statement, args)


def _notify_runs(url: str, worker: Worker) -> None:
    """`PASSES` writes to the live run's row, as its harness writes it, each one waited on until the worker has passed."""
    for _ in range(PASSES):
        seen = len(worker.passes())
        _sql(url, "UPDATE runs SET status = 'running' WHERE run_id = 'n1-1'")  # a statement on runs: one notification
        _until(lambda seen=seen: len(worker.passes()) > seen, 120, "a pass woken by the runs write")


def _initiative_extra(url: str) -> dict:
    with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
        row = conn.execute("SELECT extra_json FROM work_items WHERE kind = 'initiative' AND task_id = 'n1'").fetchone()
    assert row is not None, "no initiative row for n1"
    return row[0] if isinstance(row[0], dict) else json.loads(row[0])


def test_a_decompose_live_on_another_machine_is_never_launched_again(launchable):
    sandbox = launchable.sandbox
    _ready_one_intake(launchable)
    _sql(sandbox.store_url, "INSERT INTO runs (run_id, launched_at, started_at, status, host) VALUES ('n1-1', %s, %s, 'running', 'far')",
         _now(), _now())
    worker = Worker(sandbox)
    try:
        _until(worker.passes, 180, "the launch worker's first pass")
        _notify_runs(sandbox.store_url, worker)
        rows = _launches(sandbox.store_url)
    finally:
        said = worker.stop()
    why = f"--- worker ---\n{said}\n--- rows ---\n{json.dumps(rows, default=str)}"
    assert len([line for line in said.splitlines() if line.startswith("chair worker launch 20")]) >= PASSES + 1, why
    assert "launch_decompose" not in said and rows == [], why


def test_ready_one_intake_and_exactly_one_decompose_launch_is_recorded(launchable):
    sandbox = launchable.sandbox
    _ready_one_intake(launchable)
    worker = Worker(sandbox)
    try:
        _until(lambda: "launch_decompose done" in worker.said(), 300, "the launch worker's decompose")
        _until(lambda: any("launch_decompose done" in line for line in worker.passes()), 60, "the decompose pass's line")
        _notify_runs(sandbox.store_url, worker)
        time.sleep(2)  # a pass already woken finishes and records before the rows are read
        rows = _launches(sandbox.store_url)
    finally:
        said = worker.stop()
    why = f"--- worker ---\n{said}\n--- rows ---\n{json.dumps(rows, default=str)}"
    assert [r[2] for r in rows] == ["done"], why  # one launch, across every pass and notification
    assert said.count("launch_decompose") == 1, why
    assert _initiative_extra(sandbox.store_url).get("lane_model") == "phase", why
