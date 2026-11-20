"""The phase queue end to end: the chair fills it, `cox worker` claims from it and runs the phase through the harness.

The team cartridge stamps new initiatives `lane_model: phase` and turns the lane-prerequisite gate off, so a phase
needs `repo:app` and its tasks' own tokens but not `docker` (the doctor's lane checks depend on the images this machine
holds; the docker rule is unit-tested). `cox chair run --once` is the real tick: it plans `queue_phases`, never a
launch, and writes the row. Each worker is a real `cox worker` process with a name of its own, against the test's
Postgres; times are the store's. The scripted runner stands in for the model, as in test_harness_launch.py.

Every `cox` here runs with an `ssh` first on PATH that records its argv and exits 1: nothing may reach a worker.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import replace

import pytest

from smoke import plan
from smoke.conftest import TEAM

pytest.importorskip("psycopg")

INITIATIVE, NOTE = "ship", plan.SMOKE_NOTE
LEASE_S = 60  # harness.phase_pick.LEASE_S


def _ssh_trap(sandbox):
    """The sandbox with an `ssh` ahead of every other on PATH that writes its argv to a file and fails."""
    bin_dir = sandbox.home / "trap-bin"
    bin_dir.mkdir(exist_ok=True)
    record = sandbox.home / "ssh-calls.txt"
    trap = bin_dir / "ssh"
    trap.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {record}\nexit 1\n', encoding="utf-8")
    trap.chmod(0o755)
    return replace(sandbox, env={**sandbox.env, "PATH": f"{bin_dir}:{sandbox.env['PATH']}"}), record


def _phase_cartridge(launchable) -> None:
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    cartridge["policy"] = {"dispatch": {"lane_model": "phase", "lane_prereqs": False}}
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")


def _filed(sandbox, requires: str = "") -> None:
    sandbox.write(f"work/{INITIATIVE}/initiative.md",
                  plan.work_item_text({"id": INITIATIVE, "title": "Ship", "repo": "~/src/app"}, "Ship the note."))
    fields = {"id": "t1", "title": "Add the note", "state": "ready", "phase": "p1", "repo": "app",
              **({"requires": requires} if requires else {})}
    sandbox.write(f"work/{INITIATIVE}/p1/t1.md", plan.work_item_text(fields, f"Add {NOTE} holding ok."))
    filled = sandbox.cox("route", "store-fill")
    assert filled.returncode == 0, filled.stdout + filled.stderr


def _queued(launchable, sandbox, requires: str = "") -> dict:
    """A phase initiative filed and one chair tick run: the phase's row, as `cox worker status --json` reads it."""
    _phase_cartridge(launchable)
    _filed(sandbox, requires)
    launchable.scripted(plan.scripted_epic_responses(plan.new_file_patch(NOTE, "ok"), [NOTE]))
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert "launch_epic" not in tick.stdout, tick.stdout
    row = _row(sandbox)
    assert row is not None, tick.stdout + tick.stderr
    return row


def _status(sandbox) -> dict:
    done = sandbox.cox("worker", "status", "--json", cwd=sandbox.workspace)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def _row(sandbox) -> dict | None:
    return next((r for r in _status(sandbox)["phases"] if (r["initiative"], r["phase"]) == (INITIATIVE, "p1")), None)


def _worker(sandbox, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(sandbox.install.cox), "worker", *args], env=sandbox.env, cwd=sandbox.workspace, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )


def _finished(proc: subprocess.Popen[str], timeout: float = 300) -> str:
    out, _ = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, out
    return out


def _until(check, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(1)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def test_two_workers_at_once_claim_the_phase_once_and_one_runs_it_to_approval_without_ssh(launchable, repo):
    sandbox, calls = _ssh_trap(launchable.sandbox)
    row = _queued(launchable, sandbox)
    assert (row["state"], row["claim_epoch"], row["requires_json"]) == ("ready", 0, ["repo:app"]), row
    again = sandbox.cox("chair", "run", "--once", timeout=180)  # a repeat tick writes nothing
    assert again.returncode == 0 and "queue_phases" not in again.stdout, again.stdout

    procs = [_worker(sandbox, "once", "--name", name) for name in ("w1", "w2")]
    outs = {name: _finished(proc) for name, proc in zip(("w1", "w2"), procs, strict=True)}
    claimed = [name for name, out in outs.items() if f"{name} claimed {INITIATIVE}/p1" in out]
    assert len(claimed) == 1, outs
    (winner,) = claimed
    row = _row(sandbox)
    assert (row["state"], row["outcome"], row["claim_epoch"], row["attempts"], row["updated_by"]) == (
        "finished", "approved", 1, 1, winner), (row, outs)
    assert row["run_id"] == f"{INITIATIVE}.p1-1", row
    assert sandbox.store_row(INITIATIVE, "t1")["state"] == "approved"
    assert sandbox.git(repo.origin, "show", f"epic/{INITIATIVE}/p1:{NOTE}") == "ok"
    assert {w["name"] for w in _status(sandbox)["workers"]} == {"w1", "w2"}
    assert not calls.exists() or calls.read_text(encoding="utf-8") == "", calls.read_text(encoding="utf-8")


def test_a_macos_phase_is_claimed_only_by_a_worker_that_advertises_it(launchable, repo):
    sandbox, _ = _ssh_trap(launchable.sandbox)
    row = _queued(launchable, sandbox, "os:macos")
    assert row["requires_json"] == ["os:macos", "repo:app"], row

    linux = _finished(_worker(sandbox, "once", "--name", "linux-1", "--capability", "os:linux"))
    assert "claimed" not in linux, linux
    assert (_row(sandbox)["state"], _row(sandbox)["claim_epoch"]) == ("ready", 0)

    mac = _finished(_worker(sandbox, "once", "--name", "mac-1", "--capability", "os:macos"))
    assert f"mac-1 claimed {INITIATIVE}/p1" in mac, mac
    row = _row(sandbox)
    assert (row["claim_epoch"], row["updated_by"], row["outcome"]) == (1, "mac-1", "approved"), (row, mac)


def test_a_worker_that_stops_heartbeating_loses_its_claim_and_its_late_write_changes_nothing(launchable, repo):
    sandbox, calls = _ssh_trap(launchable.sandbox)
    _queued(launchable, sandbox)
    stale = _worker(sandbox, "run", "--name", "stale")
    try:
        held = _until(lambda: (r := _row(sandbox)) and r["state"] == "claimed" and r["run_id"] and r, 120, "the first claim")
        assert (held["claimed_by"], held["claim_epoch"]) == ("stale", 1), held
        os.killpg(stale.pid, signal.SIGSTOP)  # the worker hangs: no heartbeat, no write, its harness run goes on alone
        _until(lambda: _status(sandbox)["now"] > held["lease_until"], LEASE_S + 30, "the lease to lapse by the store's clock")
        assert _row(sandbox)["state"] == "claimed"  # no sweeper: the lapsed claim is simply claimable

        fresh = _finished(_worker(sandbox, "once", "--name", "fresh"))
        assert f"fresh claimed {INITIATIVE}/p1" in fresh, fresh
        taken = _row(sandbox)
        assert (taken["claim_epoch"], taken["updated_by"]) == (2, "fresh"), (taken, fresh)

        os.killpg(stale.pid, signal.SIGCONT)  # it wakes and writes under its old epoch
        time.sleep(5)  # its next pass (a poll is at most 2 s) heartbeats first, and finds the claim gone
        stale.send_signal(signal.SIGINT)
        out, _ = stale.communicate(timeout=60)
        # Whichever write it had next (a heartbeat, or the finish of a run that ended while it hung), it holds no claim.
        late = (f"lost {INITIATIVE}/p1 (epoch 1)", f"{INITIATIVE}/p1 (epoch 1) is no longer held; nothing written")
        assert any(line in out for line in late), out
        assert _row(sandbox) == taken, out
    finally:
        if stale.poll() is None:
            os.killpg(stale.pid, signal.SIGCONT)
            stale.kill()
            stale.wait(timeout=30)
    assert not calls.exists() or calls.read_text(encoding="utf-8") == "", calls.read_text(encoding="utf-8")
