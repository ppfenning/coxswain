"""L7, lost-worker recovery, end to end: worker A dies mid-phase, killed (SIGKILL) with its store address gone dark, and
worker B, once A's lease lapses by the store's clock, claims the phase and carries on from where the branch and the
store stand (`lane_progress.resume_plan`, `review_loop.next`), building and reviewing nothing twice.

The machine is `test_phase_lane.py`'s (`lane_machine`): three tasks t1, t2 and t3 (t3 needing both) of phase p1. Both
workers run with `--lease-s 48`, the shortest lease a worker accepts, so the wait for A's lease to lapse is the
worker's own setting, not a patch. A reaches the store only through a relay of the test's (`Relay`), named in A's own
provider profile (`AGENT_TOOLS_PROFILE`, so the lane A starts inherits it). B reaches the store directly. The relay
loses A's store in one of two ways: blackholed (`silence`: every socket stays open and every byte is dropped, so
nothing is refused or reset and only a bound of the lane's own can end a call), or cut (`cut`: the port and every
connection through it closed, as a machine that lost its network leaves them).

- A is killed after two of the three tasks rolled up, with t3 in its check, and its store blackholed: A's lane gives
  up its claim check at its bound and stops itself, and its children with it, all before A's lease lapses and so
  before B can claim; the chair marks the lapsed claim's run lost and tells the operator once; `cox worker status`
  shows the lapsed claim; B builds t3 alone and the phase is reviewed once, its branch holding exactly the three task
  commits.
- A is killed in review round 2, during its smoke, and its store cut: B's first step is round 2's smoke, then round 2's
  review.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import threading
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from smoke import plan
from smoke import test_phase_lane as lanes
from smoke.test_phase_lane import (
    APPROVE,
    INITIATIVE,
    PHASE,
    Worker,
    _alive,
    _answers,
    _commits,
    _lane_pid,
    _owner,
    _reviews,
    _row,
    _run_log,
    _steps,
    _store_now,
    _until,
)
from smoke.test_phase_review import REVISE, _build, _phase_steps, _rounds

lane_machine = lanes.lane_machine  # the machine these tests request, as a fixture of this module too

LEASE_S = 48  # `--lease-s`: harness.phase_pick.MIN_LEASE_S
CHECK_CALL_S = 3  # harness.phase_pick.CHECK_CALL_S: how long the lane's claim check waits on a silent store


class Relay:
    """A TCP relay to the store on a loopback port of its own: worker A's store address. `silence` blackholes it: every
    connection, open or new, stays open and every byte either way is dropped. `cut` closes the port and shuts every
    connection through it."""

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        self._target = (parts.hostname or "127.0.0.1", parts.port or 5432)
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.5)
        self.port = self._server.getsockname()[1]
        self._open: list[socket.socket] = []
        self._lock, self._cut, self._silent = threading.Lock(), threading.Event(), threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def url(self, url: str) -> str:
        parts = urlsplit(url)
        userinfo = parts.netloc.rpartition("@")[0]
        return urlunsplit(parts._replace(netloc=f"{userinfo}@127.0.0.1:{self.port}" if userinfo else f"127.0.0.1:{self.port}"))

    def _accept(self) -> None:
        try:
            while not self._cut.is_set():
                try:
                    near, _ = self._server.accept()
                except TimeoutError:
                    continue
                self._relay(near)
        except OSError:
            return
        finally:
            self._server.close()

    def _relay(self, near: socket.socket) -> None:
        if self._silent.is_set():  # blackholed: the connection is taken and held, and nothing it sends goes anywhere
            with self._lock:
                self._open.append(near)
            threading.Thread(target=self._drain, args=(near,), daemon=True).start()
            return
        try:
            far = socket.create_connection(self._target, timeout=10)
        except OSError:
            near.close()
            return
        near.settimeout(None)
        far.settimeout(None)
        with self._lock:
            if self._cut.is_set():
                near.close()
                far.close()
                return
            self._open += [near, far]
        for src, dst in ((near, far), (far, near)):
            threading.Thread(target=self._pump, args=(src, dst), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while data := src.recv(65536):
                if not self._silent.is_set():
                    dst.sendall(data)
        if self._silent.is_set() and not self._cut.is_set():
            return  # a blackhole closes nothing: the other side's socket stays open until the test cuts it
        for s in (src, dst):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)

    @staticmethod
    def _drain(near: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while near.recv(65536):
                pass

    def silence(self) -> None:
        self._silent.set()

    def cut(self) -> None:
        self._cut.set()
        self._server.close()
        with self._lock:
            for s in self._open:
                with contextlib.suppress(OSError):
                    s.shutdown(socket.SHUT_RDWR)
                s.close()
        _until(self._refused, 10, "the relay's port to close")

    def _refused(self) -> bool:
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()
        except OSError:
            return True
        return False


def _through(sandbox, env: dict[str, str], relay: Relay) -> dict[str, str]:
    """`env` with a routing profile of its own whose provider profile names the store through `relay`: A's config."""
    config = sandbox.home / ".config" / "agent-tools"
    provider = (config / "provider.yaml").read_text(encoding="utf-8")
    assert sandbox.store_url in provider, provider
    (config / "provider-a.yaml").write_text(provider.replace(sandbox.store_url, relay.url(sandbox.store_url)), encoding="utf-8")
    routing = (config / "profile.yaml").read_text(encoding="utf-8")
    assert str(config / "provider.yaml") in routing, routing
    (config / "profile-a.yaml").write_text(routing.replace(str(config / "provider.yaml"), str(config / "provider-a.yaml")),
                                           encoding="utf-8")
    return {**env, "AGENT_TOOLS_PROFILE": str(config / "profile-a.yaml")}


def _seconds(earlier: str, later: str) -> int:
    def at(stamp: str) -> datetime:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    return int((at(later) - at(earlier)).total_seconds())


def _descendants(pid: int) -> set[int]:
    """Every live process under `pid`, its children's children too, by `pgrep -P` (Linux and macOS)."""
    found: set[int] = set()
    todo = [pid]
    while todo:
        done = subprocess.run(["pgrep", "-P", str(todo.pop())], capture_output=True, text=True, check=False)
        children = {int(line) for line in done.stdout.split() if line.isdigit()} - found
        found |= children
        todo += children
    return found


def _lose(sandbox, worker: Worker, relay: Relay, *, blackhole: bool = False) -> tuple[str, int, dict]:
    """Kill worker A and lose its store address, blackholed or cut; its lane, cut off, stops itself, and every process
    under it, before the lease lapses: before anyone else may claim the phase. The run, the lane's pid, and the claim
    as it stood."""
    run_id = _row(sandbox)["run_id"]
    lane = _lane_pid(sandbox, run_id)
    tree = _descendants(lane)
    worker.kill()
    if blackhole:
        relay.silence()
    else:
        relay.cut()
    held = _row(sandbox)  # read once A can no longer renew it: its last heartbeat is in
    [(beat_at, until)] = _owner(sandbox, "SELECT heartbeat_at, lease_until FROM phase_queue WHERE initiative = %s AND "
                                         "phase = %s", args=(INITIATIVE, PHASE))[0]
    assert _seconds(beat_at, until) == LEASE_S, (beat_at, until)  # the claim is on the worker's own lease
    try:
        _until(lambda: not _alive(lane) and not [p for p in tree if _alive(p)], LEASE_S,
               "A's lane and every process under it to stop")
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{_run_log(sandbox, run_id)}") from None
    assert _store_now(sandbox) < held["lease_until"], held  # it stopped itself: nobody could take the claim yet
    log = _run_log(sandbox, run_id)
    assert "lane: stopping: the claim could not be read" in log, log
    if blackhole:  # nothing refused or reset the call: the check's own bound ended it
        assert f"StoreDeadline: the store did not answer within {CHECK_CALL_S}s" in log, log
    _until(lambda: _store_now(sandbox) > held["lease_until"], LEASE_S + 30, "A's lease to lapse by the store's clock")
    return run_id, lane, held


def _told(sandbox) -> list[str]:
    rows = _owner(sandbox, "SELECT action_json FROM chair_actions WHERE kind = 'needs_chair' ORDER BY ts")[0]
    docs = [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]
    return [d["reason"] for d in docs if d.get("cause") == "run_lost"]


def _status(sandbox) -> tuple[str, dict]:
    text = sandbox.cox("worker", "status", cwd=sandbox.workspace)
    data = sandbox.cox("worker", "status", "--json", cwd=sandbox.workspace)
    assert text.returncode == 0 and data.returncode == 0, text.stdout + text.stderr + data.stderr
    row = next(r for r in json.loads(data.stdout)["phases"] if (r["initiative"], r["phase"]) == (INITIATIVE, PHASE))
    return text.stdout, row


def test_a_worker_killed_after_two_roll_ups_is_recovered_by_another_that_builds_only_the_third(lane_machine):
    sandbox, repo, name, env, _, launchable = lane_machine
    launchable.scripted(_answers({"t1": 1, "t2": 1, "t3": 600}))  # t3's check outlasts everything below
    relay = Relay(sandbox.store_url)
    a = Worker(sandbox, _through(sandbox, env, relay), "run", "--name", name, "--lease-s", str(LEASE_S))
    try:
        _until(lambda: {s["task_id"] for s in _steps(sandbox) if s["kind"] == "rollup"} == {"t1", "t2"}
               and [s for s in _steps(sandbox) if (s["task_id"], s["kind"]) == ("t3", "build")], 240,
               "two roll-ups and t3's build")
        lost_run, _, held = _lose(sandbox, a, relay, blackhole=True)
    finally:
        a.kill()
        relay.cut()
    assert f"heartbeat every 8s, lease {LEASE_S}s" in a.said(), a.said()

    # The chair marks the lapsed claim's run lost and tells the operator, once; `cox worker status` shows it lapsed.
    for _ in range(2):
        tick = sandbox.cox("chair", "run", "--once", timeout=180)
        assert tick.returncode == 0, tick.stdout + tick.stderr
    mark = (f"run lost: {name}'s claim at epoch 1 lapsed at {held['lease_until']}, leaving run {lost_run}; "
            "claimable again")
    assert _row(sandbox)["reason"] == mark and _row(sandbox)["state"] == "claimed", _row(sandbox)
    assert _told(sandbox) == [mark]
    text, shown = _status(sandbox)
    assert f"lapsed at {held['lease_until']}" in text and f"run {lost_run} lost (the chair marked it), claimable" in text, text
    assert (shown["claim"]["lapsed"], shown["claim"]["marked_lost"], shown["claim"]["holder"]) == (True, True, name), shown

    launchable.scripted(_answers({"t1": 1, "t2": 1, "t3": 1}))
    b = Worker(sandbox, env, "once", "--name", f"{name}-b", "--lease-s", str(LEASE_S)).finished()
    assert f"{name}-b claimed {INITIATIVE}/{PHASE} (epoch 2)" in b and "a lane of an older claim" not in b, b
    row = _row(sandbox)
    assert (row["state"], row["outcome"], row["claim_epoch"], row["attempts"]) == ("finished", "approved", 2, 2), (row, b)
    assert [s["task_id"] for s in _steps(sandbox, row["run_id"]) if s["kind"] == "build"] == ["t3"]
    commits = _commits(sandbox, repo.origin)
    assert [(c["task"], c["kind"]) for c in commits][2:] == [("t3", "task")] and len(commits) == 3, commits
    assert {c["kind"] for c in commits} == {"task"} and {c["run"] for c in commits[:2]} == {lost_run}, commits
    # Reviewed once: one round, B's, and A wrote no review at all.
    assert [r[:2] for r in _rounds(sandbox)] == [(1, "approve")]
    assert _reviews(sandbox, lost_run) == [] and _reviews(sandbox, row["run_id"]) == ["phase/review/1"]
    assert _told(sandbox) == [mark]  # B's recovery tells nothing more


# The phase level: it passes at once, but the first time it runs at a tip holding round 1's revision it marks that
# and sleeps, so A is killed in round 2's smoke; B's run of the same smoke finds the mark and passes.
HOLD_ROUND_2 = ('if [ -e notes/rev-1.txt ] && [ ! -e {marks}/round-2 ]; then touch {marks}/round-2; sleep 600; fi')


def test_a_worker_killed_in_review_round_two_is_recovered_by_another_whose_first_step_is_round_two(lane_machine):
    sandbox, repo, name, env, _, launchable = lane_machine
    marks = sandbox.home / "marks"
    (repo.path / ".agent-checks").write_text(f"# level: phase\n{HOLD_ROUND_2.format(marks=marks)}\n", encoding="utf-8")
    sandbox.git(repo.path, "add", ".agent-checks")
    sandbox.git(repo.path, "commit", "--quiet", "-m", "the phase level")
    answers = _answers({"t1": 1, "t2": 1, "t3": 1}, review=None)
    answers[f"review_phase@{PHASE}-review-1"] = REVISE
    answers[f"build@{PHASE}-rev-1"] = _build(plan.new_file_patch("notes/rev-1.txt", "rev 1"), ["notes/rev-1.txt"])
    launchable.scripted(answers)
    relay = Relay(sandbox.store_url)
    a = Worker(sandbox, _through(sandbox, env, relay), "once", "--name", name, "--lease-s", str(LEASE_S))
    try:
        _until((marks / "round-2").exists, 300, "A's lane to reach round 2's smoke")
        lost_run, _, _ = _lose(sandbox, a, relay)
    finally:
        a.kill()
        relay.cut()
    assert _phase_steps(sandbox, lost_run) == ["phase/smoke/1", "phase/review/1"]  # A's round 2 wrote nothing

    # Only round 2's review is scripted: a round 1 asked again, or a revision built again, would fail B's run.
    second = _answers({}, review=None)
    second[f"review_phase@{PHASE}-review-2"] = APPROVE
    launchable.scripted(second)
    b = Worker(sandbox, env, "once", "--name", f"{name}-b", "--lease-s", str(LEASE_S)).finished(timeout=300)
    row = _row(sandbox)
    assert (row["state"], row["outcome"], row["claim_epoch"]) == ("finished", "approved", 2), (row, b)
    assert "finished approved: the phase review approved it in round 2" in b, b
    assert not [s for s in _steps(sandbox, row["run_id"]) if s["kind"] == "build"]
    assert _phase_steps(sandbox, row["run_id"]) == ["phase/smoke/2", "phase/review/2"]
    commits = _commits(sandbox, repo.origin)
    assert [(c["task"], c["kind"]) for c in commits[3:]] == [(f"{PHASE}-rev-1", "revision")] and len(commits) == 4, commits
    log = _run_log(sandbox, row["run_id"])
    first = log.index("lane: the phase smoke at ")
    assert log[first:].startswith(f"lane: the phase smoke at {commits[3]['sha'][:12]}"), log  # round 2's tip, first
    assert "review round 1" not in log and f"building {PHASE}-rev-1" not in log, log
    assert [r[:2] for r in _rounds(sandbox)] == [(1, "revise"), (2, "approve")]
