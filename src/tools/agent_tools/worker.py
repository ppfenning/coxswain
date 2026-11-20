"""The phase worker: claims the next phase it qualifies for, runs it, keeps its claim alive, and gives it back.

`cox worker run` is a loop on one machine. Each pass it renews every claim it holds (a heartbeat every 10 s), reaps the
runs that ended (ack or nack, `phase_pick.verdict`), and claims more while it has a free lane. A claim is run through
today's harness, the per-phase launch (`cox route launch epic --local --phase P`), until the lane runner replaces it.

The chair never reaches a worker: it fills `phase_queue` and the worker pulls from it, so no ssh crosses to a worker.

A claim whose heartbeat is refused (another worker holds it, or it lapsed) is dropped at once and its run stopped. A
claim whose heartbeats keep failing (the store is unreachable) is dropped, and its run stopped, once 45 s have passed
since its last good one, before its 60 s lease can lapse: no phase is ever run by two workers at once. The worker
itself keeps polling, so it recovers when the store does.

The rules are pure here and in `harness.phase_pick`; every effect is a port (`Ports`, built by `worker_ports`).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agent_tools import lane_prereqs
from harness import phase_pick

if TYPE_CHECKING:
    from harness.store_phases import Claim

POLL_S = 2.0  # idle poll with a free lane; a claim made elsewhere is seen within this
DOCTOR_EVERY_S = 600.0  # how often the lane checks run again, so `docker` follows the machine


def free_lanes(capacity: int, own_claims: int, old_model_runs_here: int) -> int:
    """Lanes this worker may still claim: the machine's capacity, less the claims it holds and the old-model runs
    already on this machine (both models share one lane budget), never below zero."""
    return max(capacity - own_claims - old_model_runs_here, 0)


def capabilities(detected: Iterable[str], declared: Iterable[str], doctor_rows: Sequence[Mapping[str, object]]) -> list[str]:
    """What the worker advertises, sorted. Detected facts (`os:linux`, `arch:arm64`) unless the operator declared the
    same key (`--capability os:macos`): the operator's word on a machine wins. `docker` only when the doctor's lane
    checks pass (`lane_prereqs.capabilities`), whatever was declared."""
    declared_set = {t for t in declared if t != lane_prereqs.DOCKER_CAPABILITY}
    keys = {t.partition(":")[0] for t in declared_set if ":" in t}
    kept = {t for t in detected if t.partition(":")[0] not in keys}
    return sorted(kept | declared_set | lane_prereqs.capabilities(doctor_rows))


def repo_tokens(repos: Iterable[str], present: Collection[str]) -> list[str]:
    """`repo:<name>` for each queued phase's repository that has a checkout on this machine (`present`), sorted."""
    return sorted({f"repo:{r.rstrip('/').rpartition('/')[2]}" for r in repos if r and r in present})


def old_model_runs(live_runs_here: Iterable[str], claimed_runs: Collection[str]) -> int:
    """The live runs on this machine that no phase claim started: the old model's, which share its lanes."""
    return sum(1 for run in live_runs_here if run not in claimed_runs)


def launch_argv(initiative: str, phase: str, run_id: str, label: str) -> list[str]:
    """Today's per-phase launch, run in the workspace: the harness for one phase, here, under the run id the claim
    recorded. `--no-claim` because a worker never takes the landing loop."""
    return [
        "cox", "route", "launch", "epic", "--local", "--initiative", f"work/{initiative}", "--phase", phase,
        "--run-id", run_id, "--label", label, "--no-claim",
    ]


def launch_label(loop_record: Mapping[str, object] | None, own: str) -> str:
    """The label a worker's launch runs under: the landing loop's holder when one is recorded, as the chair's own launch
    commands carry it (a launch under another label is refused by a live loop), else the worker's own."""
    session = (loop_record or {}).get("session")
    return session if isinstance(session, str) and session else own


def attempts_of(claim: Claim) -> int:
    raw = claim.row.get("attempts")
    return raw if isinstance(raw, int) else 0


@dataclass(frozen=True)
class Launched:
    run_id: str
    pid: int


@dataclass(frozen=True)
class Lane:
    """One claim this worker holds and the run it started."""

    claim: Claim
    run: Launched
    last_ok: float  # this worker's monotonic clock at the last successful heartbeat
    beat_due: float


@dataclass(frozen=True)
class Ports:
    name: str
    capabilities: Callable[[], list[str]]  # what the worker advertises now; registers it when it changed
    capacity: Callable[[], int]  # the machine's lanes, 0 while its hosts row is draining
    old_runs: Callable[[], int]  # old-model live runs on this machine (`old_model_runs`)
    claim: Callable[[Sequence[str], int], Claim | None]
    heartbeat: Callable[[Claim], bool]  # True renewed, False the claim is gone; raises when the store cannot be reached
    set_run: Callable[[Claim, str], bool]
    finish: Callable[[Claim, str, str], bool]
    release: Callable[[Claim, str, int], bool]
    task_states: Callable[[str, str], list[str]]  # (initiative, phase) -> its tasks' states
    launch: Callable[[Claim, Callable[[str], bool]], Launched | str]  # the run, or why it did not start
    alive: Callable[[Launched], bool]
    stop: Callable[[Launched], None]
    unregister: Callable[[], None]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    log: Callable[[str], None]


def _key(claim: Claim) -> str:
    return f"{claim.initiative}/{claim.phase} (epoch {claim.epoch})"


def beat(ports: Ports, lanes: Sequence[Lane]) -> list[Lane]:
    """Renew each claim that is due; drop, and stop the run of, each claim that is gone or has gone too long without a
    good heartbeat (`phase_pick.must_stop`). A stopped claim gets no write: it is no longer this worker's to write."""
    return [kept for lane in lanes if (kept := _beat_one(ports, lane)) is not None]


def _beat_one(ports: Ports, lane: Lane) -> Lane | None:
    now = ports.monotonic()
    if now >= lane.beat_due:
        try:
            held = ports.heartbeat(lane.claim)
        except Exception as exc:  # the store did not answer: the claim stands until must_stop says otherwise
            ports.log(f"worker: heartbeat for {_key(lane.claim)} failed: {type(exc).__name__}: {exc}")
            lane = replace(lane, beat_due=now + phase_pick.HEARTBEAT_S)
        else:
            if not held:
                ports.log(f"worker: lost {_key(lane.claim)}: another worker holds it or its lease lapsed; stopping {lane.run.run_id}")
                ports.stop(lane.run)
                return None
            lane = replace(lane, last_ok=now, beat_due=now + phase_pick.HEARTBEAT_S)
    if phase_pick.must_stop(lane.last_ok, ports.monotonic()):
        ports.log(f"worker: no heartbeat for {_key(lane.claim)} in {phase_pick.STOP_AFTER_S}s; stopping {lane.run.run_id}")
        ports.stop(lane.run)
        return None
    return lane


def settle(ports: Ports, claim: Claim, verdict: phase_pick.Verdict) -> None:
    """Write the ack or nack; a write the claim no longer holds changes nothing, and says so."""
    if verdict.kind == "retry":
        done = ports.release(claim, verdict.reason, verdict.wait_s)
        said = f"released for a retry in {verdict.wait_s}s: {verdict.reason}"
    else:
        done = ports.finish(claim, verdict.outcome, verdict.reason)
        said = f"finished {verdict.outcome}: {verdict.reason}"
    ports.log(f"worker: {_key(claim)} {said}" if done else f"worker: {_key(claim)} is no longer held; nothing written")


def reap(ports: Ports, lanes: Sequence[Lane]) -> list[Lane]:
    """Settle each claim whose run has ended, from its tasks' states (`phase_pick.verdict`); keep the rest."""
    kept: list[Lane] = []
    for lane in lanes:
        if ports.alive(lane.run):
            kept.append(lane)
            continue
        states = ports.task_states(lane.claim.initiative, lane.claim.phase)
        settle(ports, lane.claim, phase_pick.verdict(states, attempts_of(lane.claim)))
    return kept


def fill(ports: Ports, lanes: Sequence[Lane], caps: Sequence[str]) -> list[Lane]:
    """Claim and start phases while a lane is free. A run that does not start is a retryable nack."""
    out = list(lanes)
    while (free := free_lanes(ports.capacity(), len(out), ports.old_runs())) > 0:
        claim = ports.claim(caps, free)
        if claim is None:
            break
        ports.log(f"worker: {ports.name} claimed {_key(claim)}")
        started = ports.launch(claim, lambda run_id, c=claim: ports.set_run(c, run_id))
        if isinstance(started, str):
            settle(ports, claim, phase_pick.verdict([], attempts_of(claim), launch_error=started))
            continue
        now = ports.monotonic()
        out.append(Lane(claim, started, now, now + phase_pick.HEARTBEAT_S))
    return out


def _wait(lanes: Sequence[Lane], now: float) -> float:
    due = min((lane.beat_due for lane in lanes), default=now + POLL_S)
    return max(min(due - now, POLL_S), 0.0)


def run(ports: Ports, *, once: bool = False) -> int:
    """The loop. With `once`, one claim pass, then heartbeats until every run it started has ended and been settled.
    An interrupt stops every run this worker started and gives each claim back to be claimed at once."""
    lanes: list[Lane] = []
    claimed = False
    try:
        while True:
            lanes = reap(ports, beat(ports, lanes))
            if not (once and claimed):
                lanes = fill(ports, lanes, ports.capabilities())
                claimed = True
            if once and not lanes:
                return 0
            ports.sleep(_wait(lanes, ports.monotonic()))
    except KeyboardInterrupt:
        for lane in lanes:
            ports.stop(lane.run)
            settle(ports, lane.claim, phase_pick.Verdict("retry", "", "the worker stopped", 0))
        return 0
    finally:
        ports.unregister()
