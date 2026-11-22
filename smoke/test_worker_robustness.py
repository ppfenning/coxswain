"""Worker robustness, from the E1 acceptance run of 2026-11-21, end to end: real Postgres, the real `cox worker` and
`cox chair`, the scripted runner for the model (the machine is `test_worker_claims.py`'s).

- A launch this machine refuses (a stray untracked dir in the repository's checkout) costs the phase no attempt: the
  claim goes back at once, the worker stops advertising `repo:app` and says so, the chair is told once, and once the
  checkout reads clean again the worker advertises the repository and claims the phase on its next epoch.
- A lane that lost its claim exits, children and all, even when a child ignores TERM: its worker hangs (SIGSTOP), the
  lane's fence loses the claim before its lease lapses and TERMs its check, which ignores it, and `LOST_EXIT_S` later
  the lane kills the check and exits 5, with nothing more written.
"""

from __future__ import annotations

import json
import signal

import pytest

from smoke import plan
from smoke.conftest import TEAM
from smoke.test_worker_claims import (
    NOTE,
    Worker,
    _alive,
    _filed,
    _harnesses,
    _no_ssh,
    _phase_cartridge,
    _queued,
    _row,
    _ssh_trap,
    _status,
    _until,
)

pytest.importorskip("psycopg")

LOST_EXIT_S = 30  # harness.phase_lane.LOST_EXIT_S
STOP_AFTER_S = 45  # harness.phase_pick.STOP_AFTER_S: the lane's fence loses its claim about then, at its margin


def _workers(sandbox) -> dict[str, list[str]]:
    return {str(w["name"]): list(w.get("capabilities") or w.get("capabilities_json") or []) for w in _status(sandbox)["workers"]}


def test_a_dirty_checkout_gives_the_claim_back_unspent_and_sets_the_repo_aside_until_it_is_clean(launchable, repo):
    sandbox, calls = _ssh_trap(launchable.sandbox)
    _queued(launchable, sandbox)
    stray = repo.path / "origin" / "stray.txt"  # E1's `?? origin/`
    stray.parent.mkdir()
    stray.write_text("left behind\n", encoding="utf-8")
    worker = Worker(sandbox, "run", "--name", "dirty")
    try:
        _until(lambda: "stops advertising repo:app (checkout)" in worker.said(), 120, "the refusal to set repo:app aside")
        row = _until(lambda: (r := _row(sandbox)) and r["state"] == "ready" and r, 30, "the claim to go back")
        assert (row["attempts"], row["claim_epoch"]) == (0, 1), row  # claimed once, its attempt refunded
        assert row["reason"].startswith("this machine cannot launch it (checkout): ~/src/app has uncommitted changes"), row
        assert "(no attempt counted)" in worker.said(), worker.said()
        _until(lambda: "repo:app" not in _workers(sandbox).get("dirty", ["repo:app"]), 30, "repo:app to leave the row")
        assert worker.said().count("worker: the chair ") == 1, worker.said()

        stray.unlink()
        stray.parent.rmdir()
        _until(lambda: "advertises repo:app again: its checkout reads clean" in worker.said(), 60, "repo:app to return")
        taken = _until(lambda: (r := _row(sandbox)) and r["claim_epoch"] == 2 and r, 120, "the next claim")
        assert taken["claimed_by"] == "dirty", taken
        assert worker.said().count("worker: the chair ") == 1, worker.said()
    finally:
        worker.signal(signal.SIGINT)
        worker.proc.wait(timeout=60)
        worker.kill()
    _no_ssh(calls)


def _deaf_cartridge(launchable, marks) -> None:
    """The phase cartridge with a check that records its pid, ignores TERM (its sleep inherits the ignore) and sleeps
    far past any lease."""
    _phase_cartridge(launchable)
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    cartridge["landing_areas"]["checks"].append(
        {"name": "deaf", "cmd": f"echo $$ > {marks}/deaf.pid; trap '' TERM; sleep 600"})
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")


def test_a_lane_whose_claim_is_lost_kills_a_child_that_ignores_term_and_exits(launchable, repo):
    sandbox, calls = _ssh_trap(launchable.sandbox)
    marks = sandbox.home / "marks"
    marks.mkdir()
    _deaf_cartridge(launchable, marks)
    _filed(sandbox)
    launchable.scripted(plan.scripted_epic_responses(plan.new_file_patch(NOTE, "ok"), [NOTE]))
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0 and "launch_epic" not in tick.stdout, tick.stdout + tick.stderr
    worker = Worker(sandbox, "run", "--name", "deaf")
    try:
        _until(lambda: (marks / "deaf.pid").exists() and _harnesses(sandbox), 180, "the deaf check to start")
        held = _row(sandbox)
        lane, check = _harnesses(sandbox)[0], int((marks / "deaf.pid").read_text(encoding="utf-8").strip())
        worker.signal(signal.SIGSTOP)  # nobody renews the claim, and nobody stops the lane from outside
        _until(lambda: not _alive(lane), STOP_AFTER_S + LOST_EXIT_S + 30, "the lane that lost its claim to exit")
        assert not _alive(check), "the check that ignored TERM outlived its lane"
        log = (sandbox.workspace / "runs" / f"{held['run_id']}.log").read_text(encoding="utf-8")
        assert "lane: stopping: " in log and "killing its children and exiting 5" in log, log
        after = _row(sandbox)
        assert (after["claimed_by"], after["claim_epoch"], after["attempts"]) == ("deaf", 1, 1), after
    finally:
        worker.kill()
    _no_ssh(calls)
