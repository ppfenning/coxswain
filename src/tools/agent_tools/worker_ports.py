"""The phase worker's effects: its store connections, the lane checks, the spawned launch, the heartbeat thread, and
stopping a run by process group.

Every time the queue compares is the store's (`store_phases`). This process's monotonic clock is compared only with
itself (`phase_pick.next_step`); its wall clock stamps only a migration's bookkeeping when the worker opens the store.
Every store call is bounded well inside the heartbeat window (`worker.bounded_url`), and the heartbeat thread has a
connection of its own, so no call on the main loop can hold up a heartbeat.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from agent_tools import bounded_run, chair, epic, lane_prereqs, repo_path, route, run_store, worker
from agent_tools.chair_read_run_id import next_run_id, taken_names
from harness import initiative_review, phase_pick, store_phases
from harness.store_dialect import Connection, Json
from harness.store_phases import Claim

_PROBE_TIMEOUT_S = 20.0  # the lane checks run on the main loop; the heartbeat thread never waits on them
_JOIN_S = 30.0  # how long a stopping worker waits for its heartbeat thread to finish its pass
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_OS = {"linux": "linux", "darwin": "macos"}


def detected() -> list[str]:
    """Edge. What this machine is: `os:<linux|macos>` and `arch:<amd64|arm64>`, as far as they are known."""
    system, machine = platform.system().lower(), platform.machine().lower()
    return [
        *([f"os:{_OS[system]}"] if system in _OS else []),
        *([f"arch:{_ARCH[machine]}"] if machine in _ARCH else []),
    ]


def doctor_rows() -> list[dict[str, object]]:
    """Edge. The doctor's lane checks on this machine (`lane_prereqs`), the rows `cox setup doctor` shows."""
    try:
        done = bounded_run.completed(lane_prereqs.script_argv(), timeout=_PROBE_TIMEOUT_S, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return [dict(r) for r in lane_prereqs.reply_rows(127, str(exc), remote=False)]
    return [dict(r) for r in lane_prereqs.reply_rows(done.returncode, done.stdout, remote=False)]


def _checkouts(repos: Sequence[str], home: str) -> list[str]:
    """Edge. The repositories, of those named, that have a git checkout on this machine."""
    return [r for r in repos if (Path(repo_path.expand(r, home)) / ".git").exists()]


class Store:
    """One connection, opened on first use and opened again after a failure, so a store restart costs one call. Every
    statement, lock wait and connect on it is bounded well inside the heartbeat window (`worker.bounded_url`), by the
    server and, for a server gone silent, by the client too (`worker.STATEMENT_S`, `Connection.limit`). With `call_s`
    each whole call ends within that or raises: its statements under `Connection.within`, and a reopen, its connect
    and version check, under `store_dialect.open_within` with what is left of the same deadline (the heartbeat's
    `phase_pick.HEARTBEAT_CALL_S`).

    Only the first open of a `migrate` store may migrate (`open_store`): the worker's main store, opened before the
    worker claims anything. Every reopen, and every open of a store that is not `migrate` (the heartbeat's), is scoped
    (`store_scoped.open_scoped`): it checks the version and writes no DDL, so no migration ever runs under a claim."""

    def __init__(self, runs_dir: Path, call_s: float | None = None, *, migrate: bool = True) -> None:
        self._runs_dir = runs_dir
        self._call_s = call_s
        self._migrate = migrate
        self._conn: Connection | None = None

    def _open(self, seconds: float | None) -> Connection:
        if self._conn is None:
            from harness.store_dialect import open_within
            from harness.store_migrate import open_store
            from harness.store_scoped import open_scoped

            url = worker.bounded_url(run_store.store_url_for(self._runs_dir))
            if self._migrate:
                self._conn = open_store(url, time.strftime(_STAMP, time.gmtime())).limit(worker.STATEMENT_S)
                self._migrate = False
            else:
                def scoped() -> Connection:
                    return open_scoped(url).limit(worker.STATEMENT_S)

                self._conn = scoped() if seconds is None else open_within(scoped, seconds)
        return self._conn

    def call[T](self, act: Callable[[Connection], T]) -> T:
        started = time.monotonic()
        try:
            conn = self._open(self._call_s)
            if self._call_s is None:
                return act(conn)
            with conn.within(self._call_s - (time.monotonic() - started)):
                return act(conn)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def _take_lost(path: Path) -> tuple[str, bool]:
    """Edge. The lane's lost mark (`phase_lane.write_lost`), removed once read: why the lane gave its claim up ("" when
    it did not) and whether a check of its read the lease first. A mark that does not say (one in an older lane's
    plain text) is taken as unchecked: the attempt counts, the safe side."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "", False
    path.unlink(missing_ok=True)
    try:
        mark = json.loads(text)
    except ValueError:
        mark = None
    if not isinstance(mark, dict):
        return text or phase_pick.GAVE_UP, False
    why = mark.get("why")
    return (why if isinstance(why, str) and why else phase_pick.GAVE_UP), mark.get("checked") is True


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _gone(alive: Callable[[], bool], wait_s: float) -> bool:
    """Edge. Whether `alive` turns false within `wait_s`, polled on this process's monotonic clock."""
    deadline = time.monotonic() + wait_s
    while alive():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def _pid_alive(pid: int) -> bool:
    """Edge. Whether `pid` is a live process (a zombie awaiting its reaper counts as gone where /proc says so)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (OSError, IndexError):
        return True


def _children(pid: int) -> set[int]:
    """Edge. The direct children of `pid`, by `pgrep -P` (Linux and macOS); none when it cannot tell."""
    try:
        done = bounded_run.completed(["pgrep", "-P", str(pid)], timeout=phase_pick.KILL_WAIT_S, capture_output=True, text=True)
    except OSError:
        return set()
    return {int(line) for line in done.stdout.split() if line.isdigit()}


def _pid_in(pidfile: Path) -> int | None:
    try:
        text = pidfile.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def build(ws: Path, *, name: str, declared: Sequence[str], lanes: int | None, version: str,
          lease_s: int = phase_pick.LEASE_S,
          log: Callable[[str], None] = lambda line: print(line, flush=True)) -> tuple[worker.Ports, Store]:
    """Edge. The ports of a worker named `name` on the workspace `ws`, claiming on a `lease_s` lease (a valid one:
    `phase_pick.lease_problem`), and the store it holds open."""
    timings = phase_pick.Timings.of(lease_s)
    runs_dir = ws / "runs"
    launches_dir = ws / "worker-launches"  # each launch's own output, beside the workspace's runs, not among them
    store = Store(runs_dir)
    home = os.path.expanduser("~")
    host = socket.gethostname()
    registered: list[str] = []
    probed: list[list[dict[str, object]]] = []  # the last lane check's rows, one entry once it has run
    probed_at = [0.0]
    launchers: dict[int, subprocess.Popen[bytes]] = {}
    launchers_lock = threading.Lock()  # the main loop polls a launch while the heartbeat thread may stop it

    def caps() -> list[str]:
        now = time.monotonic()
        if not probed or now - probed_at[0] >= worker.DOCTOR_EVERY_S:
            probed[:], probed_at[0] = [doctor_rows()], now
        rows = probed[0]
        queued = [str(r.get("repo") or "") for r in store.call(store_phases.list_rows) if r.get("state") != "finished"]
        found = sorted({*worker.capabilities(detected(), declared, rows), *worker.repo_tokens(queued, _checkouts(queued, home))})
        if found != registered:
            doctor_json: Json = {"lane": [{"check": str(r.get("check")), "ok": r.get("ok") is True} for r in rows]}
            store.call(lambda c: store_phases.register(c, name, found, capacity(), "up", version, doctor_json))
            registered[:] = found
            log(f"worker: {name} advertises {', '.join(found) or 'nothing'}")
        return found

    def capacity() -> int:
        from harness.store_hosts import host_list

        row = next((h for h in store.call(host_list) if h["name"] == name), None)
        if row is not None and row["state"] == "draining":
            return 0
        if lanes is not None:
            return lanes
        return int(row["capacity"]) if row is not None and isinstance(row["capacity"], int) else 1

    def old_runs() -> int:
        claimed = {str(r.get("run_id") or "") for r in store.call(store_phases.live_claims)}
        now = store.call(store_phases.store_now)
        here = [lane.run for lane in run_store.live_lanes(runs_dir, now) if lane.host in (None, "", host, name)]
        return worker.old_model_runs(here, claimed)

    def lane_facts(claim: Claim, run_id: str) -> worker.LaneFacts:
        from harness.phase_lane import lost_path

        facts = _facts(claim, run_id)  # first: a pass whose store reads fail leaves the lost mark for the next one
        why, checked = _take_lost(lost_path(runs_dir, run_id))
        return replace(facts, lost=why, checked=checked)

    def _facts(claim: Claim, run_id: str) -> worker.LaneFacts:
        from harness.store_queue import read

        if claim.row.get("kind") == initiative_review.KIND:  # the initiative's own rounds and suite (D2)
            rounds = store.call(lambda c: store_phases.review_rounds(c, claim.initiative, initiative_review.ROUNDS_PHASE))
            suites = store.call(lambda c: store_phases.suite_rows(c, claim.initiative))
            last, reason, suite = initiative_review.ended(rounds, suites)
            return worker.LaneFacts((), (), last, None, reason, suite)
        rows = store.call(lambda c: read(c, claim.initiative))
        rollups = store.call(lambda c: store_phases.rollup_rows(c, [claim.initiative]))
        rounds = store.call(lambda c: store_phases.review_rounds(c, claim.initiative, claim.phase))
        last = (int(str(rounds[-1]["round"])), str(rounds[-1]["verdict"])) if rounds else None
        smokes = store.call(lambda c: store_phases.smoke_rows(c, claim.initiative, claim.phase))
        return worker.lane_facts(rows, rollups, claim.phase, last, smokes, run_id,
                                 str(rounds[-1]["reason"] or "") if rounds else "")

    def launch(claim: Claim, record: Callable[[str], bool]) -> worker.Started | str:
        names = [p.name for p in runs_dir.iterdir()] if runs_dir.is_dir() else []
        run_id = next_run_id(route.phase_run_prefix(claim.initiative, claim.phase), taken_names(names, run_store.run_ids(runs_dir)))
        if not record(run_id):
            return "the claim was lost before its run started"
        for old in worker.stale_lanes(epic.live_runs(runs_dir, route.phase_run_prefix(claim.initiative, claim.phase)), run_id):
            log(f"worker: stopping {old.run_id} (pid {old.pid}), a lane of an older claim of {claim.initiative}/{claim.phase}")
            if not stop(old):
                return f"the older lane {old.run_id} (pid {old.pid}) did not stop"
        argv = worker.launch_argv(claim.initiative, claim.phase, run_id,
                                  worker.launch_label(chair.read(runs_dir), f"worker:{name}"), name, claim.epoch)
        argv[0] = shutil.which("cox") or argv[0]
        launches_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(launches_dir / f"{run_id}.out", "wb") as out:
                proc = subprocess.Popen(argv, cwd=ws, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        except OSError as exc:
            return f"the launch did not spawn: {type(exc).__name__}: {exc}"
        with launchers_lock:
            launchers[proc.pid] = proc
        log(f"worker: launching {run_id} for {claim.initiative}/{claim.phase}")
        return worker.Started(run_id, proc.pid)

    def launched(started: worker.Started) -> worker.Running | tuple[int, str] | None:
        with launchers_lock:
            proc = launchers.get(started.launcher)
            code = proc.poll() if proc is not None else -1
            if code is None:
                return None
            launchers.pop(started.launcher, None)
        pid = _pid_in(runs_dir / f"{started.run_id}.pid")
        if code == 0 and pid is not None:
            log(f"worker: started {started.run_id} (pid {pid})")
            return worker.Running(started.run_id, pid)
        try:
            output = (launches_dir / f"{started.run_id}.out").read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        return code, output or ("no pidfile after the launch" if code == 0 else "")

    def alive(run: worker.Running) -> bool:
        return epic.process_alive(run.pid, runs_dir / f"{run.run_id}.pid")

    def stop(run: worker.Started | worker.Running) -> bool:
        """End the run within `STOP_GRACE_S + KILL_WAIT_S`; True once it is gone.

        A launch still running is frozen first (SIGSTOP), so it cannot start the harness after its children are read;
        each child it has (the harness, in a session of its own) and the harness its pidfile names are sent TERM, the
        launch is killed, and the harnesses get KILL if they outlast the grace."""
        pids: set[int] = set()
        if isinstance(run, worker.Started):
            with launchers_lock:
                proc = launchers.pop(run.launcher, None)
            if proc is not None and proc.poll() is None:
                _signal_group(proc.pid, signal.SIGSTOP)
                pids |= _children(proc.pid)
                _signal_group(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=phase_pick.KILL_WAIT_S)
            if (named := _pid_in(runs_dir / f"{run.run_id}.pid")) is not None:
                pids.add(named)
        else:
            # The lane, and each child it started in a session of its own (a check), whose group the lane's own
            # group signal does not reach: a lane killed while frozen leaves no check running on.
            pids |= {run.pid, *_children(run.pid)}
        for pid in pids:
            _signal_group(pid, signal.SIGTERM)
        if _gone(lambda: any(_pid_alive(p) for p in pids), phase_pick.STOP_GRACE_S):
            return True
        for pid in pids:
            _signal_group(pid, signal.SIGKILL)
        return _gone(lambda: any(_pid_alive(p) for p in pids), phase_pick.KILL_WAIT_S)

    def lane_mark(run: worker.Running) -> str:
        from harness.phase_lane import beat_path

        try:
            return beat_path(runs_dir, run.run_id).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def heartbeats(leases: worker.Leases) -> Callable[[], None]:
        """A thread of its own, on a store connection of its own, renewing each lease when it is due."""
        own = Store(runs_dir, call_s=phase_pick.HEARTBEAT_CALL_S, migrate=False)
        halt = threading.Event()

        def renew(claim: Claim) -> bool:
            return own.call(lambda c: store_phases.heartbeat(c, claim, name, timings.lease_s)) is not None

        request_stop = worker.stopper(stop, leases, log)

        def loop() -> None:
            try:
                while not halt.wait(phase_pick.BEAT_LOOP_S):
                    worker.beat(leases, renew, request_stop, time.monotonic, log)
            finally:
                own.close()

        thread = threading.Thread(target=loop, name=f"worker-heartbeat-{name}", daemon=True)
        thread.start()

        def halt_and_join() -> None:
            halt.set()
            if thread.is_alive():
                thread.join(timeout=_JOIN_S)

        return halt_and_join

    def unregister() -> None:
        try:
            store.call(lambda c: store_phases.register(c, name, registered, 0, "stopping", version, {}))
        except Exception as exc:  # the store is gone: the row's beat ages, which says the same
            log(f"worker: {name} could not mark itself stopping: {type(exc).__name__}: {exc}")

    ports = worker.Ports(
        name=name,
        capabilities=caps,
        capacity=capacity,
        old_runs=old_runs,
        claim=lambda capabilities, free: store.call(
            lambda c: store_phases.claim_next(c, name, capabilities, free, timings.lease_s)
        ),
        set_run=lambda claim, run_id: store.call(lambda c: store_phases.set_run(c, claim, name, run_id)),
        finish=lambda claim, outcome, reason: store.call(lambda c: store_phases.finish(c, claim, name, outcome, reason)),
        release=lambda claim, reason, wait_s, refund: store.call(
            lambda c: store_phases.release(c, claim, name, reason, wait_s, refund=refund)
        ),
        lane_facts=lane_facts,
        lane_mark=lane_mark,
        launch=launch,
        launched=launched,
        alive=alive,
        stop=stop,
        heartbeats=heartbeats,
        unregister=unregister,
        monotonic=time.monotonic,
        sleep=time.sleep,
        log=log,
        timings=timings,
    )
    return ports, store


def status(conn: Connection) -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    """Edge. The queue, the registry and the store's time, as `cox worker status` shows them."""
    return store_phases.list_rows(conn), store_phases.list_workers(conn), store_phases.store_now(conn)
