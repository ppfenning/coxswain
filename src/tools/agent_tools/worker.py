"""The phase worker: claims the next phase it qualifies for, runs it, keeps its claim alive, and gives it back.

`cox worker run` is a loop on one machine; `Leases` is what it holds. The main loop claims while a lane is free,
starts each claim's run through today's per-phase launch (`cox route launch epic --local --phase P`, spawned, never
waited on), and settles each run that ends (ack or nack, `phase_pick.verdict`). A heartbeat thread of its own, on a
connection of its own, renews every claim every 10 s (`beat`), whatever the main loop is doing: a slow claim, a lane
check or a store call can hold up the next claim, never a heartbeat.

A claim's clock starts when the worker sets out to claim it. A claim whose heartbeat is refused (another worker holds
it, or it lapsed) is `lost`; one with no good heartbeat for 45 s is `expired`, which is before its 60 s lease can
lapse. A worker given another lease (`--lease-s`, 48 s to 600 s, `phase_pick.Timings`) scales all three. Either way the heartbeat thread stops its run at once (the launch and the harness, by process group, until
they are gone) and the main loop drops it without a write: it is no longer this worker's to write. So no phase is
ever run by two workers at once.

An interrupt or SIGTERM stops every run this worker started, confirms each is gone, then gives each claim it still
holds back without costing an attempt.

The chair never reaches a worker: it fills `phase_queue` and the worker pulls from it, so no ssh crosses to a worker.
The rules are pure here and in `harness.phase_pick`; every effect is a port (`Ports`, built by `worker_ports`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agent_tools import lane_prereqs, machine_refusal
from harness import initiative_review, phase_pick

if TYPE_CHECKING:
    from harness.store_phases import Claim

POLL_S = 2.0  # idle poll with a free lane; a claim made elsewhere is seen within this
DOCTOR_EVERY_S = 600.0  # how often the lane checks run again, so `docker` follows the machine
STATEMENT_TIMEOUT_MS = 5_000  # a worker's store call: well inside the 45 s it may go without a heartbeat
LOCK_TIMEOUT_MS = 3_000
CONNECT_TIMEOUT_S = 5
STATEMENT_S = STATEMENT_TIMEOUT_MS / 1000 + 1  # the client's own bound on one statement, past the server's

class NameTaken(Exception):
    """Another machine's live worker is registered under this worker's name (`worker_identity.takeover`), or took the
    row over: the worker stops rather than share one identity."""


# `cox route launch` exits 2 with one of these when a gate refused the launch, not because the phase is broken.
REFUSALS = (
    "is already running",  # a live run of this phase holds its `runs:<initiative>.<phase>` lease
    "is fenced",
    "routing: usage stop",  # the spend gate
    "refusing: the landing loop is held by",  # the label guard
)


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


def bounded_url(url: str) -> str:
    """A Postgres URL whose every statement, lock wait and connect is bounded well inside the heartbeat window: libpq
    takes `connect_timeout` from the query, and the URL's own `options` are applied after the store's defaults
    (`store_dialect.merged_options`), so these win. Any other URL is returned as it is."""
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres", "postgresql+psycopg"):
        return url
    query = dict(parse_qsl(parts.query))
    bounds = f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}"
    query["options"] = f"{query['options']} {bounds}" if query.get("options") else bounds
    query.setdefault("connect_timeout", str(CONNECT_TIMEOUT_S))
    return urlunsplit(parts._replace(query=urlencode(query)))


def launch_argv(initiative: str, phase: str, run_id: str, label: str, worker: str, epoch: int) -> list[str]:
    """The phase lane's launch, run in the workspace: the harness's `phase-lane` for the one claimed phase, here,
    under the run id the claim recorded and the claim's epoch, which every write of the lane is fenced on.
    `--no-claim` because a worker never takes the landing loop."""
    return [
        "cox", "route", "launch", "epic", "--local", "--initiative", f"work/{initiative}", "--phase", phase,
        "--run-id", run_id, "--label", label, "--no-claim", "--lane-worker", worker, "--claim-epoch", str(epoch),
    ]


def launch_label(loop_record: Mapping[str, object] | None, own: str) -> str:
    """The label a worker's launch runs under: the landing loop's holder when one is recorded, as the chair's own launch
    commands carry it (a launch under another label is refused by a live loop), else the worker's own."""
    session = (loop_record or {}).get("session")
    return session if isinstance(session, str) and session else own


def refused(returncode: int, output: str) -> bool:
    """Whether a launch that exited `returncode` was refused by a gate (`REFUSALS`) rather than failing."""
    return returncode == 2 and any(marker in output for marker in REFUSALS)


def launch_verdict(returncode: int, output: str, attempts: int,
                   prior: str = "") -> tuple[phase_pick.Verdict, machine_refusal.Refusal | None]:
    """The nack for a launch that exited before its lane started, and the refusal of this machine's that caused it, if
    one did. This machine's refusal (`machine_refusal.of`: a dirty or missing checkout, a guard of the machine's)
    gives the claim back at once with its attempt refunded, `machine_refusal.REFUNDS` times an episode (counted on the
    claim's `prior` reason; past them it counts), and the worker sets the repository aside; a gate's
    (`refused`) gives it back refunded after the backoff; anything else is a failed launch, a counted attempt."""
    last = next((line for line in reversed(output.splitlines()) if line.strip()), "")
    found = machine_refusal.of(returncode, output)
    if found is not None:
        return machine_refusal.verdict(found, attempts, prior), found
    if refused(returncode, output):
        return phase_pick.refusal(f"the launch was refused: {last}", attempts), None
    return phase_pick.verdict([], attempts, launch_error=f"the launch exited {returncode}: {last}"), None


def attempts_of(claim: Claim) -> int:
    raw = claim.row.get("attempts")
    return raw if isinstance(raw, int) else 0


def key_of(claim: Claim) -> str:
    return f"{claim.initiative}/{claim.phase} (epoch {claim.epoch})"


def stale_lanes(live: Sequence[Mapping[str, object]], own_run: str) -> list[Running]:
    """Every live lane of the phase on this machine (`epic.live_runs` of its run prefix) other than the run of the
    claim just taken: one claim runs at a time, so each is an older claim's, frozen or orphaned, and is stopped before
    the new lane starts, rather than refusing the new launch for as long as it lives."""
    return [Running(str(r["run"]), int(str(r["pid"]))) for r in live if r.get("run") != own_run]


@dataclass(frozen=True)
class LaneFacts:
    """What the store holds for a phase once its lane ended: its tasks (dropped ones left out), the tasks whose roll-up
    put a commit on the phase branch in any run, its last review round as (round, verdict), and the phase smoke the
    ended run wrote last, None when it wrote none; and that round's reason, which a quarantine carries."""

    tasks: tuple[str, ...]
    rolled_up: tuple[str, ...]
    last_round: tuple[int, str] | None
    smoke: phase_pick.SmokeSeen | None
    reason: str = ""
    suite: initiative_review.SuiteSeen | None = None  # an initiative review's: the suite at the tip its round reviewed
    lost: str = ""  # why the lane gave its claim up (its lost mark, `phase_lane.lost_path`), "" when it did not
    checked: bool = False  # whether a check of that lane read its lease before the loss (the lost mark says)
    revision: str = ""  # why a last `revise` round's revision task did not roll up (`phase_pick.revision_failure`)


def lane_verdict(claim: Claim, facts: LaneFacts) -> phase_pick.Verdict:
    """The ack or nack for a claim whose lane ended: an initiative review's by its round and suite
    (`initiative_review.lane_verdict`), any other phase's by its tasks, round and smoke (`phase_pick.lane_verdict`).

    A lane that gave its claim up (`facts.lost`) while this worker still holds it (`settle` writes nothing otherwise)
    after a good check lost it to the store, not to the phase: its attempt is not counted, up to
    `phase_pick.LOST_REFUNDS` times an episode (`phase_pick.lost_refund`). What the store holds still decides an ack
    or a real failure; any retry, including one the attempts would otherwise have ended, goes back with the attempt
    refunded (`phase_pick.refusal`) and the episode's count one up on the row's reason. A loss at the first check, or
    one past the cap, counts as any other attempt, and its retry's reason still says the lane gave its claim up."""
    prior = str(claim.row.get("reason") or "")
    refunds = phase_pick.refunds_of(prior)[0]
    refund = bool(facts.lost) and phase_pick.lost_refund(prior, facts.checked)
    attempts = attempts_of(claim) - (1 if refund else 0)
    if claim.row.get("kind") == initiative_review.KIND:
        found = initiative_review.lane_verdict(facts.last_round, facts.suite, attempts, reason=facts.reason)
    else:
        found = phase_pick.lane_verdict(facts.tasks, facts.rolled_up, facts.last_round, attempts,
                                        smoke=facts.smoke, reason=facts.reason, revision=facts.revision)
    if not facts.lost or found.kind != "retry":
        return found
    if refund:
        why = f"{phase_pick.GAVE_UP} ({facts.lost}); {found.reason}"
        return phase_pick.refusal(phase_pick.with_refunds(why, refunds + 1), attempts_of(claim))
    counted = ("at its first check, before it read its lease" if not facts.checked
               else f"after {refunds} refunded this episode")
    why = f"{phase_pick.GAVE_UP} ({facts.lost}); counted, {counted}; {found.reason}"
    return replace(found, reason=phase_pick.with_refunds(why, *phase_pick.refunds_of(prior)))


def lane_facts(task_rows: Sequence[Mapping[str, object]], rollups: Sequence[Mapping[str, object]], phase: str,
               last_round: tuple[int, str] | None, smokes: Sequence[Mapping[str, object]], run_id: str,
               reason: str = "", revision_task: str = "",
               revision_rows: Sequence[Mapping[str, object]] = ()) -> LaneFacts:
    """The `LaneFacts` of `phase`, from its initiative's work-item rows and roll-up step rows, and the phase's `smoke`
    step rows of every run, oldest first (`store_phases.smoke_rows`), of which the ended run is `run_id`. A last round
    of `revise` names its revision task (`revision_task`), whose step rows of the ended run (`revision_rows`) say why
    it did not roll up, unless a roll-up of any run holds it."""
    tasks = sorted(str(r["task_id"]) for r in task_rows
                   if r.get("kind") == "task" and r.get("phase") == phase and r.get("state") != "dropped")
    rolled = sorted({str(r["task_id"]) for r in rollups if r.get("phase") == phase})
    own = [r for r in smokes if r.get("run_id") == run_id]
    pending = last_round is not None and last_round[1] == "revise" and bool(revision_task) and revision_task not in rolled
    return LaneFacts(tuple(tasks), tuple(rolled), last_round, _smoke_of(own[-1], smokes) if own else None, reason,
                     revision=phase_pick.revision_failure(revision_rows) if pending else "")


def _fault(row: Mapping[str, object]) -> bool:
    result = row.get("result_json")
    return row.get("ok") not in (1, True) and isinstance(result, Mapping) and result.get("harness_fault") is True


def _smoke_of(row: Mapping[str, object], smokes: Sequence[Mapping[str, object]]) -> phase_pick.SmokeSeen:
    """A `smoke` step row as the worker reads it; a row with no reason says its verdict. A harness fault counts every
    harness fault of the phase at the same tip, in any run."""
    result = row.get("result_json")
    reason = str(result.get("reason") or "") if isinstance(result, Mapping) else ""
    faults = sum(1 for r in smokes if _fault(r) and r.get("subject_sha") == row.get("subject_sha")) if _fault(row) else 0
    return phase_pick.SmokeSeen(row.get("ok") in (1, True),
                                reason or f"the phase smoke was {row.get('verdict') or 'not recorded'}", faults)


@dataclass(frozen=True)
class Started:
    """A launch spawned and not yet settled: the run id it was given and the launcher's pid."""

    run_id: str
    launcher: int


@dataclass(frozen=True)
class Running:
    run_id: str
    pid: int


type Run = Started | Running
State = Literal["held", "lost", "expired"]


@dataclass
class Held:
    """One claim this worker holds. `last_ok` is this worker's monotonic clock when it set out to claim, then at each
    good heartbeat; `state` moves from held only on the heartbeat thread, and then the claim is no longer this worker's."""

    claim: Claim
    last_ok: float
    beat_due: float
    run: Run | None = None
    state: State = "held"
    stopped: bool = False  # set once the heartbeat thread has stopped the run of a claim it gave up on
    mark: tuple[str, float] | None = None  # the lane's progress mark as last read, and when it last moved


@dataclass
class Leases:
    """The claims this worker holds, shared by the main loop and the heartbeat thread under one lock."""

    entries: dict[str, Held] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    timings: phase_pick.Timings = phase_pick.DEFAULT_TIMINGS

    def add(self, claim: Claim, started: float, run: Run | None = None) -> None:
        with self.lock:
            self.entries[key_of(claim)] = Held(claim, started, started + self.timings.heartbeat_s, run)

    def set_run(self, claim: Claim, run: Run) -> None:
        with self.lock:
            if (held := self.entries.get(key_of(claim))) is not None:
                held.run = run

    def snapshot(self) -> list[Held]:
        with self.lock:
            return [Held(h.claim, h.last_ok, h.beat_due, h.run, h.state, h.stopped, h.mark) for h in self.entries.values()]

    def see(self, claim: Claim, mark: tuple[str, float]) -> None:
        with self.lock:
            if (held := self.entries.get(key_of(claim))) is not None:
                held.mark = mark

    def take(self, claim: Claim) -> Held | None:
        """Remove the claim and return it, only while it is still held: one the heartbeat thread gave up on is
        removed too, but returned as None, so nothing is written for it."""
        with self.lock:
            held = self.entries.pop(key_of(claim), None)
            return held if held is not None and held.state == "held" else None

    def mark_stopped(self, claim: Claim) -> None:
        with self.lock:
            if (held := self.entries.get(key_of(claim))) is not None:
                held.stopped = True

    def __len__(self) -> int:
        with self.lock:
            return len(self.entries)


type RequestStop = Callable[[Claim, Run | None], None]


def beat(leases: Leases, heartbeat: Callable[[Claim], bool], request_stop: RequestStop, now: Callable[[], float],
         log: Callable[[str], None]) -> None:
    """One pass of the heartbeat loop over every held claim, as `phase_pick.next_step` says: renew one that is due;
    mark `lost` one whose heartbeat is refused, and `expired` one whose stop time has come or whose due heartbeat
    could run past it. A marked claim's run is handed to `request_stop`, which returns at once (`stopper`), so a
    slow stop never holds up another claim's heartbeat. A heartbeat that raises (the store did not answer within its
    bound) leaves the claim held until its stop time. Every time here is the leases' own (`Leases.timings`)."""
    timings = leases.timings
    for held in leases.snapshot():
        if held.state != "held":
            continue
        step = phase_pick.next_step(held.last_ok, now(), held.beat_due, timings)
        if step == "wait":
            continue
        ok_at, due = None, held.beat_due
        state: State = "expired" if step == "stop" else "held"
        if step == "beat":
            at = now()
            due = at + timings.heartbeat_s
            try:
                renewed = heartbeat(held.claim)
            except Exception as exc:  # the store did not answer within its bound: the stop time decides
                log(f"worker: heartbeat for {key_of(held.claim)} failed: {type(exc).__name__}: {exc}")
                renewed = None
            ok_at = at if renewed is True else None  # the time it was sent: the lease runs from no later than that
            state = "lost" if renewed is False else "held"
            if state == "held" and ok_at is None and now() >= phase_pick.stop_at(held.last_ok, timings):
                state = "expired"
        run = _record_beat(leases, held.claim, ok_at, due, state)
        if state != "held" and run is not False:
            why = "another worker holds it or its lease lapsed" if state == "lost" else "no good heartbeat in time"
            log(f"worker: {state} {key_of(held.claim)}: {why}; stopping its run")
            request_stop(held.claim, run)


def stopper(stop: Callable[[Run], bool], leases: Leases, log: Callable[[str], None]) -> RequestStop:
    """The `request_stop` the heartbeat loop hands a claim it gave up on: each stop runs on a thread of its own, and
    marks the claim stopped once its run is gone, so the main loop frees its lane only then."""

    def request(claim: Claim, run: Run | None) -> None:
        def work() -> None:
            try:
                if run is not None and not stop(run):
                    log(f"worker: the run of {key_of(claim)} did not stop")
            finally:
                leases.mark_stopped(claim)

        threading.Thread(target=work, name=f"worker-stop-{claim.phase}", daemon=True).start()

    return request


def _record_beat(leases: Leases, claim: Claim, ok_at: float | None, due: float, state: State) -> Run | Literal[False] | None:
    """Write the pass's result into the claim; its run as it is now, or False when the main loop already took it."""
    with leases.lock:
        held = leases.entries.get(key_of(claim))
        if held is None or held.state != "held":
            return False
        if ok_at is not None:
            held.last_ok = ok_at
        held.beat_due, held.state = due, state
        return held.run


@dataclass(frozen=True)
class Ports:
    name: str
    capabilities: Callable[[], list[str]]  # what the worker advertises now; registers it when it changed
    capacity: Callable[[], int]  # the machine's lanes, 0 while its hosts row is draining
    old_runs: Callable[[], int]  # old-model live runs on this machine (`old_model_runs`)
    claim: Callable[[Sequence[str], int], Claim | None]
    set_run: Callable[[Claim, str], bool]
    finish: Callable[[Claim, str, str], bool]
    release: Callable[[Claim, str, int, bool], bool]  # (claim, reason, wait_s, refund)
    lane_facts: Callable[[Claim, str], LaneFacts]  # what the store holds for the claimed phase once its run ended
    lane_mark: Callable[[Running], str]  # the lane's progress mark as it reads now, "" while there is none
    launch: Callable[[Claim, Callable[[str], bool]], Started | str]  # the spawned launch, or why it did not spawn
    launched: Callable[[Started], Running | tuple[int, str] | None]  # the run, the launch's (exit, output), or None while it runs
    alive: Callable[[Running], bool]
    stop: Callable[[Run], bool]  # stop the launch and the harness by process group; True once both are gone
    heartbeats: Callable[[Leases], Callable[[], None]]  # start the heartbeat thread over the leases; returns its stop
    unregister: Callable[[], None]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    log: Callable[[str], None]
    set_aside: Callable[[Claim, machine_refusal.Refusal], None]  # stop advertising the claim's repo; tell the chair once
    timings: phase_pick.Timings = phase_pick.DEFAULT_TIMINGS  # the lease it claims on and the heartbeat and stop it keeps


def settle(ports: Ports, leases: Leases, claim: Claim, verdict: phase_pick.Verdict) -> None:
    """Write the ack or nack, while the claim is still held; a claim the heartbeat thread gave up on gets no write. The
    write is fenced in the store as well, so one that lost its claim meanwhile changes nothing, and says so."""
    if leases.take(claim) is None:
        ports.log(f"worker: {key_of(claim)} is no longer held; nothing written")
        return
    if verdict.kind == "retry":  # any retry keeps the episode's refund counts: lost lanes', machines' refusals'
        prior = str(claim.row.get("reason") or "")
        verdict = replace(verdict, reason=phase_pick.carry_refunds(prior, machine_refusal.carry_refunds(prior, verdict.reason)))
        done = ports.release(claim, verdict.reason, verdict.wait_s, verdict.refund)
        said = f"released for a retry in {verdict.wait_s}s{' (no attempt counted)' if verdict.refund else ''}: {verdict.reason}"
    else:
        done = ports.finish(claim, verdict.outcome, verdict.reason)
        said = f"finished {verdict.outcome}: {verdict.reason}"
    ports.log(f"worker: {key_of(claim)} {said}" if done else f"worker: {key_of(claim)} is no longer held; nothing written")


def reap(ports: Ports, leases: Leases) -> None:
    """Drop each claim the heartbeat thread gave up on once its run is stopped (until then it still fills a lane);
    move each spawned launch on to its run or settle it (`launch_verdict`: a refusal of the gates' or of this machine's
    costs no attempt, and this machine's sets the claim's repository aside); settle each run that
    ended, from what the store holds for its phase; and stop a live lane whose progress mark has stood still for
    `phase_pick.LANE_STALL_S` (`phase_pick.lane_stalled`), a hung lane under this live worker, and give its claim back
    as a counted attempt."""
    for held in leases.snapshot():
        claim = held.claim
        if held.state != "held":
            if held.stopped:
                leases.take(claim)
            continue
        if isinstance(held.run, Started):
            found = ports.launched(held.run)
            if isinstance(found, Running):
                leases.set_run(claim, found)
            elif found is not None:
                launch, refusal = launch_verdict(*found, attempts_of(claim), str(claim.row.get("reason") or ""))
                if refusal is not None:  # before the claim goes back, so this worker's next pass cannot take it again
                    ports.set_aside(claim, refusal)
                settle(ports, leases, claim, launch)
        elif isinstance(held.run, Running) and not ports.alive(held.run):
            settle(ports, leases, claim, lane_verdict(claim, ports.lane_facts(claim, held.run.run_id)))
        elif isinstance(held.run, Running):
            mark, stalled = phase_pick.lane_stalled(held.mark, ports.lane_mark(held.run), ports.monotonic())
            leases.see(claim, mark)
            if not stalled:
                continue
            ports.log(f"worker: the lane of {key_of(claim)} made no progress mark in {phase_pick.LANE_STALL_S}s; stopping it")
            if ports.stop(held.run):
                why = f"the lane made no progress mark in {phase_pick.LANE_STALL_S}s and was stopped"
                settle(ports, leases, claim, phase_pick.Verdict("retry", "", why, phase_pick.backoff_s(attempts_of(claim))))
            else:
                ports.log(f"worker: the hung lane of {key_of(claim)} did not stop")


def fill(ports: Ports, leases: Leases, caps: Sequence[str]) -> int:
    """Claim and spawn phases while a lane is free; the claims made. A launch that does not spawn is a retryable nack."""
    made = 0
    while (free := free_lanes(ports.capacity(), len(leases), ports.old_runs())) > 0:
        started = ports.monotonic()  # the claim's clock starts before the claim, so it stops before the lease lapses
        claim = ports.claim(caps, free)
        if claim is None:
            break
        made += 1
        ports.log(f"worker: {ports.name} claimed {key_of(claim)}")
        spawned = ports.launch(claim, lambda run_id, c=claim: ports.set_run(c, run_id))
        # The heartbeat loop first sees the claim with its launch already on it, so a stop always has a run to end;
        # its clock still runs from `started`, before the claim.
        leases.add(claim, started, None if isinstance(spawned, str) else spawned)
        if isinstance(spawned, str):
            settle(ports, leases, claim, phase_pick.verdict([], attempts_of(claim), launch_error=spawned))
    return made


def run(ports: Ports, *, once: bool = False) -> int:
    """The loop. With `once`, one claim pass, then until every run it started has ended and been settled.
    An interrupt (SIGINT, or SIGTERM made one by the CLI) stops every run this worker started, confirms each is gone,
    and gives each claim it still holds back without costing an attempt. `NameTaken` (another machine's live worker
    has this name, `ports.capabilities`) does the same, then is raised: the worker never runs under a shared name."""
    leases = Leases(timings=ports.timings)
    stop_beats = ports.heartbeats(leases)
    claimed = False
    try:
        while True:
            try:
                reap(ports, leases)
                # Every pass, `once` too after its claim: it is where a row another machine took over is noticed.
                capabilities = ports.capabilities()
                if not (once and claimed):
                    fill(ports, leases, capabilities)
                    claimed = True
            except NameTaken:
                raise
            except Exception as exc:  # a store call that timed out: the next pass reads everything again
                ports.log(f"worker: pass failed: {type(exc).__name__}: {exc}")
            if once and claimed and not len(leases):
                return 0
            ports.sleep(POLL_S)
    except (KeyboardInterrupt, NameTaken) as exc:
        stop_beats()
        for held in leases.snapshot():
            gone = held.run is None or ports.stop(held.run)
            if gone:
                settle(ports, leases, held.claim, phase_pick.Verdict("retry", "", "the worker was stopped", 0, refund=True))
            else:
                ports.log(f"worker: the run of {key_of(held.claim)} did not stop; its claim is left to lapse")
        if isinstance(exc, NameTaken):
            raise
        return 0
    finally:
        stop_beats()
        ports.unregister()
