"""The phase queue end to end: the chair fills it, `cox worker` claims from it and runs the phase's lane.

The team cartridge stamps new initiatives `lane_model: phase` and turns the lane-prerequisite gate off, so a phase
needs `repo:app` and its tasks' own tokens but not `docker` (the doctor's lane checks depend on the images this machine
holds; the docker rule is unit-tested). `cox chair run --once` is the real tick: it plans `queue_phases`, never a
launch, and writes the row. Each worker is a real `cox worker` process with a name of its own, against the test's
Postgres; times are the store's. The scripted runner stands in for the model, as in test_harness_launch.py. Where a
test needs a run that lasts, the cartridge gains a check that sleeps, so the harness is still running while the test
takes its lease away.

Every `cox` here runs with an `ssh` first on PATH that records its argv and exits 1: nothing may reach a worker.

The lane is L1's (`harness.phase_lane`): it takes no run lease, and a phase whose every task rolled up waits for its
phase review, which is L6's, so a built phase goes back to the queue rather than finishing approved. Where a test needs
it approved, it writes the review round itself as the store's owner (`_approve`), as L6 will.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from smoke import plan
from smoke.conftest import TEAM

psycopg = pytest.importorskip("psycopg")

INITIATIVE, NOTE = "ship", plan.SMOKE_NOTE
LEASE_S = 60  # harness.phase_pick.LEASE_S
STOP_AFTER_S = 45  # harness.phase_pick.STOP_AFTER_S
SLOW_S = 240  # the sleeping check: far longer than any lease, so only a stop ends the run


def _ssh_trap(sandbox):
    """The sandbox with an `ssh` ahead of every other on PATH that writes its argv to a file and fails."""
    bin_dir = sandbox.home / "trap-bin"
    bin_dir.mkdir(exist_ok=True)
    record = sandbox.home / "ssh-calls.txt"
    trap = bin_dir / "ssh"
    trap.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {record}\nexit 1\n', encoding="utf-8")
    trap.chmod(0o755)
    return replace(sandbox, env={**sandbox.env, "PATH": f"{bin_dir}:{sandbox.env['PATH']}"}), record


def _no_ssh(record: Path) -> None:
    assert not record.exists() or record.read_text(encoding="utf-8") == "", record.read_text(encoding="utf-8")


def _phase_cartridge(launchable, *, slow: bool = False) -> None:
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    cartridge["policy"] = {"dispatch": {"lane_model": "phase", "lane_prereqs": False}}
    if slow:
        cartridge["landing_areas"]["checks"].append({"name": "slow", "cmd": f"sleep {SLOW_S}"})
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")


def _filed(sandbox, requires: str = "", phases: tuple[str, ...] = ("p1",)) -> None:
    sandbox.write(f"work/{INITIATIVE}/initiative.md",
                  plan.work_item_text({"id": INITIATIVE, "title": "Ship", "repo": "~/src/app"}, "Ship the note."))
    for n, phase in enumerate(phases, 1):
        fields = {"id": f"t{n}", "title": f"Add the note {n}", "state": "ready", "phase": phase, "repo": "app",
                  **({"requires": requires} if requires else {})}
        sandbox.write(f"work/{INITIATIVE}/{phase}/t{n}.md", plan.work_item_text(fields, f"Add {NOTE} holding ok."))
    filled = sandbox.cox("route", "store-fill")
    assert filled.returncode == 0, filled.stdout + filled.stderr


def _queued(launchable, sandbox, requires: str = "", *, slow: bool = False, phases: tuple[str, ...] = ("p1",)) -> dict:
    """A phase initiative filed and one chair tick run: the first phase's row, as `cox worker status --json` reads it."""
    _phase_cartridge(launchable, slow=slow)
    _filed(sandbox, requires, phases)
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


def _row(sandbox, phase: str = "p1") -> dict | None:
    return next((r for r in _status(sandbox)["phases"] if (r["initiative"], r["phase"]) == (INITIATIVE, phase)), None)


class Worker:
    """A `cox worker` process writing to a log file of its own, so a test reads what it said while it runs."""

    def __init__(self, sandbox, *args: str) -> None:
        self.log = sandbox.home / f"worker-{len(list(sandbox.home.glob('worker-*.log')))}.log"
        with open(self.log, "w", encoding="utf-8") as out:
            self.proc = subprocess.Popen(
                [str(sandbox.install.cox), "worker", *args], env=sandbox.env, cwd=sandbox.workspace, text=True,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )

    def said(self) -> str:
        return self.log.read_text(encoding="utf-8")

    def finished(self, timeout: float = 300) -> str:
        assert self.proc.wait(timeout=timeout) == 0, self.said()
        return self.said()

    def signal(self, sig: int) -> None:
        os.killpg(self.proc.pid, sig)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.signal(signal.SIGCONT)
            self.signal(signal.SIGKILL)
            self.proc.wait(timeout=30)


def _until(check, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(1)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a harness that exited but is not yet reaped by whoever parents it counts as gone
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def _harnesses(sandbox) -> list[int]:
    """The live harness processes of this phase's runs, from their pidfiles."""
    pids = []
    for pidfile in (sandbox.workspace / "runs").glob(f"{INITIATIVE}.p1-*.pid"):
        text = pidfile.read_text(encoding="utf-8").strip()
        if text.isdigit() and _alive(int(text)):
            pids.append(int(text))
    return pids


class MostAtOnce:
    """Samples the live harnesses twice a second on a thread of its own; `most` is the largest number seen at once."""

    def __init__(self, sandbox) -> None:
        self.most, self._sandbox, self._halt = 0, sandbox, threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        while not self._halt.wait(0.5):
            self.most = max(self.most, len(_harnesses(self._sandbox)))

    def stop(self) -> int:
        self._halt.set()
        self._thread.join(timeout=5)
        return self.most


def _approve(sandbox, phase: str = "p1") -> None:
    """The scripted round, as L6 will write it: the phase review approved; and the phase claimable at once."""
    with psycopg.connect(sandbox.store_url, autocommit=True, connect_timeout=10) as conn:
        conn.execute("INSERT INTO review_rounds VALUES (%s, %s, 1, '', 'approve', '', '{}', '', 'smoke', 'smoke', 0, 0, '')",
                     (INITIATIVE, phase))
        conn.execute("UPDATE phase_queue SET ready_at = '2000-01-01T00:00:00Z' WHERE initiative = %s AND phase = %s",
                     (INITIATIVE, phase))


def _store_clock(sandbox) -> str:
    with psycopg.connect(sandbox.store_url, connect_timeout=10) as conn:
        row = conn.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')").fetchone()
    return str(row[0])


def test_two_workers_at_once_claim_the_phase_once_and_one_runs_it_to_approval_without_ssh(launchable, repo):
    sandbox, calls = _ssh_trap(launchable.sandbox)
    row = _queued(launchable, sandbox)
    assert (row["state"], row["claim_epoch"], row["requires_json"]) == ("ready", 0, ["repo:app"]), row
    again = sandbox.cox("chair", "run", "--once", timeout=180)  # a repeat tick writes nothing
    assert again.returncode == 0 and "queue_phases" not in again.stdout, again.stdout

    workers = {name: Worker(sandbox, "once", "--name", name) for name in ("w1", "w2")}
    outs = {name: w.finished() for name, w in workers.items()}
    claimed = [name for name, out in outs.items() if f"{name} claimed {INITIATIVE}/p1" in out]
    assert len(claimed) == 1, outs
    (winner,) = claimed
    row = _row(sandbox)
    # Built and given back to wait for its review, without costing an attempt.
    assert (row["state"], row["outcome"], row["claim_epoch"], row["attempts"], row["updated_by"]) == (
        "ready", "", 1, 0, winner), (row, outs)
    assert "waiting for the phase review" in row["reason"], row
    assert row["run_id"] == f"{INITIATIVE}.p1-1", row
    assert sandbox.git(repo.origin, "show", f"epic/{INITIATIVE}/p1:{NOTE}") == "ok"
    assert {w["name"] for w in _status(sandbox)["workers"]} == {"w1", "w2"}
    _no_ssh(calls)


def test_a_macos_phase_is_claimed_only_by_a_worker_that_advertises_it(launchable, repo):
    sandbox, _ = _ssh_trap(launchable.sandbox)
    row = _queued(launchable, sandbox, "os:macos")
    assert row["requires_json"] == ["os:macos", "repo:app"], row

    linux = Worker(sandbox, "once", "--name", "linux-1", "--capability", "os:linux").finished()
    assert "claimed" not in linux, linux
    assert (_row(sandbox)["state"], _row(sandbox)["claim_epoch"]) == ("ready", 0)

    mac = Worker(sandbox, "once", "--name", "mac-1", "--capability", "os:macos").finished()
    assert f"mac-1 claimed {INITIATIVE}/p1" in mac, mac
    row = _row(sandbox)
    assert (row["claim_epoch"], row["updated_by"], row["state"]) == (1, "mac-1", "ready"), (row, mac)


def test_a_worker_with_one_lane_never_holds_two_claims(launchable, repo):
    sandbox, _ = _ssh_trap(launchable.sandbox)
    _queued(launchable, sandbox, phases=("p1", "p2"))
    assert {_row(sandbox, p)["state"] for p in ("p1", "p2")} == {"ready"}

    one = Worker(sandbox, "once", "--name", "solo", "--lanes", "1")
    held: list[int] = []
    while one.proc.poll() is None:
        held.append(sum(1 for r in _status(sandbox)["phases"] if r["state"] == "claimed"))
        time.sleep(0.5)
    out = one.finished()
    assert out.count("solo claimed") == 1, out
    assert max(held, default=0) <= 1, held
    rows = {p: _row(sandbox, p) for p in ("p1", "p2")}
    assert sorted(r["claim_epoch"] for r in rows.values()) == [0, 1], rows  # one built and waiting, one never claimed


def test_a_fleet_of_one_machine_takes_a_phase_initiative_from_queue_to_main(launchable, repo):
    """The chair queues, the worker builds, the review approves, the chair approves the tasks, the lander merges and
    lands: one machine, one pass of each."""
    sandbox, calls = _ssh_trap(launchable.sandbox)
    _queued(launchable, sandbox)
    built = Worker(sandbox, "once", "--name", "solo").finished()
    assert "waiting for the phase review" in built, built
    _approve(sandbox)
    reviewed = Worker(sandbox, "once", "--name", "solo").finished()
    assert "finished approved" in reviewed, reviewed
    approving = sandbox.cox("chair", "run", "--once", timeout=180)
    assert approving.returncode == 0, approving.stdout + approving.stderr
    assert sandbox.store_row(INITIATIVE, "t1")["state"] == "approved"
    sandbox.write("runs/policy.gate.json", json.dumps({"level": "full"}))  # the lander merges its own pull request
    adopted = sandbox.cox("lander", "adopt", "--apply", INITIATIVE, timeout=300)
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    landed = sandbox.cox("lander", "once", timeout=300)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    on_main = sandbox.git(repo.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert NOTE in on_main, adopted.stdout + landed.stdout + landed.stderr
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0 and "launch_epic" not in tick.stdout and "relaunch" not in tick.stdout, tick.stdout
    _no_ssh(calls)


def test_a_hung_worker_loses_its_claim_and_two_lanes_never_run_at_once(launchable, repo):
    """The worker hangs (SIGSTOP) with its lane running, so neither it nor its heartbeat thread can act. The lane, in
    a session of its own, sees its claim go unrenewed by the store's clock and stops itself before the lease lapses
    (it takes no run lease of its own to hold anything up). A second worker then claims and runs its own lane, which
    is stopped when that worker is told to stop. When the first worker wakes it is past its stop time: it gives the
    claim up and writes nothing."""
    sandbox, calls = _ssh_trap(launchable.sandbox)
    _queued(launchable, sandbox, slow=True)
    watch = MostAtOnce(sandbox)
    stale = Worker(sandbox, "run", "--name", "stale")
    fresh = None
    try:
        held = _until(lambda: (r := _row(sandbox)) and r["state"] == "claimed" and r["run_id"] and _harnesses(sandbox) and r,
                      120, "the first claim's lane")
        assert (held["claimed_by"], held["claim_epoch"]) == ("stale", 1), held
        stale.signal(signal.SIGSTOP)
        _until(lambda: not _harnesses(sandbox), LEASE_S + 30, "the unrenewed lane to stop itself")
        assert _store_clock(sandbox) < held["lease_until"], held  # gone before its claim could be taken over
        _until(lambda: _status(sandbox)["now"] > held["lease_until"], LEASE_S + 30, "the lease to lapse by the store's clock")
        assert _row(sandbox)["state"] == "claimed"  # no sweeper: the lapsed claim is simply claimable

        fresh = Worker(sandbox, "run", "--name", "fresh", "--lanes", "2")
        taken = _until(lambda: (r := _row(sandbox)) and r["claim_epoch"] == 2 and _harnesses(sandbox) and r, 120,
                       "the second lane")
        assert taken["claimed_by"] == "fresh", taken

        stale.signal(signal.SIGCONT)
        # Woken long past its stop time, it gives the claim up without sending a heartbeat it could not finish in time.
        _until(lambda: f"expired {INITIATIVE}/p1 (epoch 1)" in stale.said(), 60, "the woken worker to give its claim up")
        stale.signal(signal.SIGINT)
        out = stale.finished(timeout=60)
        now = _row(sandbox)
        assert (now["claimed_by"], now["claim_epoch"], now["attempts"]) == ("fresh", 2, 2), (now, out)  # it wrote nothing

        fresh.signal(signal.SIGINT)
        out = fresh.finished(timeout=60)
        assert f"fresh claimed {INITIATIVE}/p1 (epoch 2)" in out and "(no attempt counted)" in out, out
    finally:
        stale.kill()
        if fresh is not None:
            fresh.kill()
    assert watch.stop() == 1
    _no_ssh(calls)


def test_a_worker_whose_heartbeat_is_blocked_stops_its_harness_before_its_lease_lapses(launchable, repo):
    """A row lock held from another session blocks every heartbeat (each times out on the worker's bounded lock wait),
    while the worker itself runs on. 45 s after its last good heartbeat it stops its harness: the store's clock is
    still inside the lease when the harness is gone, and the worker writes nothing for the claim after."""
    sandbox, calls = _ssh_trap(launchable.sandbox)
    _queued(launchable, sandbox, slow=True)
    worker = Worker(sandbox, "run", "--name", "blocked")
    try:
        _until(lambda: (r := _row(sandbox)) and r["state"] == "claimed" and _harnesses(sandbox) and r, 120, "the harness")
        with psycopg.connect(sandbox.store_url, connect_timeout=10) as lock:
            lock.execute("SELECT phase FROM phase_queue WHERE initiative = %s AND phase = 'p1' FOR UPDATE", (INITIATIVE,))
            leased = _row(sandbox)  # a plain read is not blocked by the lock
            _until(lambda: not _harnesses(sandbox), STOP_AFTER_S + 30, "the worker to stop its harness")
            assert _store_clock(sandbox) < leased["lease_until"], (leased, worker.said())
            lock.rollback()
        assert f"expired {INITIATIVE}/p1 (epoch 1)" in worker.said(), worker.said()
        worker.signal(signal.SIGINT)
        out = worker.finished(timeout=60)
        row = _row(sandbox)
        assert (row["state"], row["claimed_by"], row["claim_epoch"], row["attempts"]) == ("claimed", "blocked", 1, 1), (row, out)
    finally:
        worker.kill()
    _no_ssh(calls)
