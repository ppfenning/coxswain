"""The chair tick loop: beat, gather, plan, perform, report and sleep, all through injected deps."""
from __future__ import annotations

import contextlib
import json
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from agent_tools import (
    chair_exec,
    chair_land,
    chair_report,
    chair_tick_status,
    chair_timing,
    dash_chair_action,
    land_limit,
    land_repo_lease,
    usage_meter,
)
from agent_tools.chair_beat import beat_in_thread
from agent_tools.chair_exec import Result
from agent_tools.chair_facts import FactsDeps, gather_facts
from agent_tools.chair_plan import TickPlan, plan_tick, plan_tick_held, reported_waiting
from agent_tools.chair_report import EASTERN, format_status, write_status
from agent_tools.chair_restart import (
    RESTART,
    WAIT,
    CheckoutState,
    Pending,
    coalesce,
    restart_decision,
    restart_line,
    restart_reason,
    touches_chair_code,
)
from agent_tools.chair_restart_log import with_note
from agent_tools.chair_types import Action, Facts, PlanTick
from agent_tools.ci_gate import DEFAULT_QUEUED_BOUND_SECONDS, CiGate, evaluate_gate
from agent_tools.forge_status import StatusReader
from agent_tools.notify_cards import card_events, pending_cards
from agent_tools.notify_core import NotifyConfig
from agent_tools.notify_dispatch import dispatch
from agent_tools.notify_events import events_from_tick

__all__ = [
    "DEFAULT_INTERVAL", "FACTS_BOUND_SECONDS", "LAND_BEAT_INTERVAL", "RELEASE_HOLD_OFF_SECONDS", "TAKEOVER_GRACE_SECONDS", "GateState",
    "LeaseRejected", "OverrunState", "ReasonState", "ReleaseState", "RestartState", "RunDeps", "TickOverrun",
    "WorkerLands", "as_holder", "error_line", "finished_land", "land_sink", "next_gate_record", "no_landing", "no_lands_in_flight", "no_lands_to_stop", "overran",
    "overrun_limit_hit", "overrun_line", "run", "tick", "tick_timed",
]

DEFAULT_INTERVAL = 60.0
PROFILE_STALE_NOTE = "profile re-read failed; keeping the last good profile"  # a segment of the one status line
LAND_BEAT_INTERVAL = 30.0  # half a minute, so the chair lease is beaten at least once a minute while a land runs
FACTS_BOUND_SECONDS = 180.0  # wall clock the facts stage may take before the tick is abandoned as an overrun
TAKEOVER_GRACE_SECONDS = 20.0  # how long a takeover waits for the holder to release before it forces the lease
RELEASE_HOLD_OFF_SECONDS = 3 * TAKEOVER_GRACE_SECONDS  # after a released chair, ticks skip the beat and lease take this long

Gather = Callable[[FactsDeps, datetime], Facts]
Perform = Callable[[list[Action], chair_exec.Deps, Callable[[], int], bool], list[Result]]
PlanHeld = Callable[[Facts, datetime, CiGate], TickPlan]


def _read_meter_doc() -> dict | None:
    """The raw JSON of the local meter file, so a fresh reading forwards unchanged; None when `usage_meter.read` finds none."""
    if usage_meter.read() is None:
        return None
    try:
        doc = json.loads(usage_meter.DEFAULT_PATH.read_text())
    except (OSError, ValueError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return None
    return doc if isinstance(doc, dict) else None


def no_lands_to_stop() -> None:
    """The stop hook of a chair with no land worker."""


def no_lands_in_flight() -> bool:
    """The in-flight source of a chair with no land worker."""
    return False


def no_landing() -> list[dict[str, str]]:
    """The landing source of a chair with no land worker."""
    return []


def no_gate_change(_gate: CiGate) -> None:
    """The set_gate hook of a chair with no land worker."""


def no_queued_lands() -> tuple[float, ...]:
    """The queued-since source of a chair with no land worker."""
    return ()


def sigterm_as_interrupt() -> Callable[[], None]:
    """Edge. SIGTERM raises KeyboardInterrupt in the main thread; the callable returned puts the old handler back."""

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, interrupt)
    except ValueError:  # signal handlers can only be set from the main thread
        return lambda: None
    return lambda: signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


@dataclass
class GateState:
    """What the CI gate was when the last acting tick finished; the one thing a tick carries to the next."""

    paused: bool = False


@dataclass
class OverrunState:
    """Facts-stage overruns in a row; reset by any tick that completes. Read by whatever decides to stop the loop."""

    consecutive: int = 0


@dataclass
class RestartState:
    """The (old, new) heads and the reason when the loop stopped because its checkout moved; None otherwise. Read after `run` returns."""

    moved: tuple[str, str] | None = None
    reason: str | None = None


@dataclass
class ReleaseState:
    """Monotonic seconds of the last takeover release, and the beat thread's stop event so a release can silence it."""

    released_at: float | None = None
    beating: threading.Event | None = None


@dataclass
class PullState:
    """The last workspace-pull failure this loop recorded; "" after a pull that succeeded."""

    last_reason: str = ""


LaneKey = tuple[str, str]  # (host, cause); the cause names the checkout, so each checkout on a host has its own key


@dataclass
class LaneSyncState:
    """The keys of the lane checkout actions already recorded and still standing."""

    seen: frozenset[LaneKey] = frozenset()


class LaneTick(NamedTuple):
    """`fresh` is to be recorded, `standing` is every action the sync reported, `keys` is what `seen` becomes once recorded."""

    fresh: list[Action]
    standing: list[Action]
    keys: frozenset[LaneKey]


@dataclass
class ReasonState:
    """The why-not-launched lines of the last tick; cleared as `tick_timed` starts, so a failed tick leaves none."""

    lines: list[str] = field(default_factory=list)


class TickOverrun(Exception):
    """The facts stage did not return inside its bound; the tick was abandoned before plan and perform."""

    def __init__(self, bound: float) -> None:
        super().__init__(f"facts stage over {bound:g}s")
        self.bound = bound


def overran(elapsed: float, bound: float) -> bool:
    return elapsed >= bound


def overrun_limit_hit(consecutive: int, limit: int) -> bool:
    """A limit of 0 or less disables the stop."""
    return limit > 0 and consecutive >= limit


def overrun_line(bound: float, count: int, now: datetime) -> str:
    return f"chair {now.astimezone(EASTERN):%m-%d %H:%M %Z} | overrun: facts stage over {bound:g}s ({count} in a row)"


@dataclass(frozen=True)
class RunDeps:
    """`beat` renews the store lease; a lost renewal reaches the tick through the lease that `gather` reads next.

    `status_reader` is None for a chair with no CI gate; its fetch and clock are injected where it is built.
    `queued_since` lists epoch seconds, one per land waiting on checks; `set_gate` hands the verdict to the land worker.
    """

    facts_deps: FactsDeps
    exec_deps: chair_exec.Deps
    report_deps: chair_report.Deps
    beat: Callable[[], object]
    current_epoch: Callable[[], int]
    holds: Callable[[], bool]
    release: Callable[[], None]
    sleep: Callable[[float], None]
    now: Callable[[], datetime]
    wait: Callable[[float], bool] | None = None  # ends the between-tick pause early; None keeps the plain sleep
    gather: Gather = gather_facts
    plan: PlanTick = plan_tick
    perform: Perform = chair_exec.perform
    meter_doc: Callable[[], dict | None] = _read_meter_doc
    stop_lands: Callable[[], object] = no_lands_to_stop
    install_sigterm: Callable[[], Callable[[], None]] = sigterm_as_interrupt
    runs_dir: Path | None = None  # None writes no tick status to the lease
    read_action: Callable[[Path], dict | None] = dash_chair_action.read_current_action
    write_tick_status: Callable[[Path, int, str], bool] = chair_tick_status.write_tick_status
    status_reader: StatusReader | None = None
    queued_since: Callable[[], tuple[float, ...]] = no_queued_lands
    set_gate: Callable[[CiGate], object] = no_gate_change
    plan_held: PlanHeld = plan_tick_held
    gate_state: GateState = field(default_factory=GateState)
    overrun_state: OverrunState = field(default_factory=OverrunState)
    # Returns "" on success or a one-line failure reason; run by every live tick right after the beat. None pulls nothing.
    pull_workspace: Callable[[], str] | None = None
    pull_state: PullState = field(default_factory=PullState)
    # The needs_chair actions of one lane checkout sync; run by every live tick after the pull. None syncs nothing.
    lane_checkout_actions: Callable[[], list[dict]] | None = None
    lane_sync_state: LaneSyncState = field(default_factory=LaneSyncState)
    facts_bound: float = FACTS_BOUND_SECONDS
    checkout_state: Callable[[], CheckoutState | None] | None = None  # None disables the restart check
    started_checkout: CheckoutState | None = None
    lands_in_flight: Callable[[], bool] = no_lands_in_flight
    restart_state: RestartState = field(default_factory=RestartState)
    # Paths changed between two heads; None reads no diff, so every move is relevant. A None return is an unreadable diff, also relevant.
    changed_files: Callable[[str, str], list[str] | None] | None = None
    # A relevant move restarts once its head has held `restart_window_s`, or `restart_max_defer_s` after it was first seen; zero restarts at the first check.
    restart_window_s: float = 0.0
    restart_max_defer_s: float = 0.0
    monotonic: Callable[[], float] = time.monotonic
    restart_note: str = ""  # the "restarts N in 24h" segment appended to every tick line; "" for none
    record_restart: Callable[[str], None] | None = None  # given the reason when the loop restarts
    save_cache: Callable[[], None] | None = None  # run on every live stop, before the lease is released
    force_holder: bool = False  # a dry run plans as the holder even behind a live foreign lease; a live tick never reads it
    # Notifier: `notify` is `dispatch`'s call shape; `notify_config` None is the off state; no `workspace` skips the step.
    notify: Callable[[list, NotifyConfig | None, Path, float], list[str]] = dispatch
    notify_config: NotifyConfig | None = None
    workspace: Path | None = None
    # The lease-only renewal run from the loop's own thread; it returns a refusal line or "" and touches no tick state. None starts no thread.
    lease_beat: Callable[[], object] | None = None
    lease_beat_interval: float = LAND_BEAT_INTERVAL
    # Receives the ids of the rows a live tick changed, exports the board and commits it; returns a note for the tick line, "" for none.
    export_rows: Callable[[tuple[str, ...]], str] | None = None
    # True when a takeover asked this holder to release and the lease and lock were just released; a live tick checks it first. None checks nothing.
    release_requested: Callable[[], bool] | None = None
    release_state: ReleaseState = field(default_factory=ReleaseState)
    reason_state: ReasonState = field(default_factory=ReasonState)


def finished_land(action: Action, outcome: chair_land.LandResult[Result] | None) -> Result:
    """The tick result of a land the worker has finished; a land that raised, or lost its repo, is a result and not an error."""
    if outcome is None:
        return {"action": action, "status": "failed", "reason": "land finished without a result"}
    if isinstance(outcome.error, chair_land.LandRefused):
        return {"action": action, "status": "busy", "reason": str(outcome.error)}
    if outcome.error is not None or outcome.value is None:
        return {"action": action, "status": "failed", "reason": f"{type(outcome.error).__name__}: {outcome.error}"}
    value = outcome.value
    return {**value, "reason": f"{value['reason']}\n{outcome.warning}"} if outcome.warning else value


# A result changed a row when it is done, landed or recorded; the rows are its action's task_id, stale_tasks and intake_ids.
CHANGED_STATUSES = frozenset({"done", "landed", "recorded"})


def changed_row_ids(results: Sequence[Result]) -> tuple[str, ...]:
    """The distinct ids of the rows the results changed, in first-seen order."""
    actions = [r["action"] for r in results if r["status"] in CHANGED_STATUSES]
    ids = (row_id for a in actions for row_id in [a.get("task_id", ""), *a.get("stale_tasks", []), *a.get("intake_ids", [])])
    return tuple(dict.fromkeys(row_id for row_id in ids if row_id))


def _export_note(export: Callable[[tuple[str, ...]], str], ids: tuple[str, ...]) -> str:
    """Edge. The hook's note, or its failure as a line; a raising hook never leaves the tick."""
    try:
        return export(ids)
    except Exception as exc:  # the rows are already changed; the failure is reported, not raised
        return f"export failed: {type(exc).__name__}: {exc}"


class WorkerLands:
    """Edge. Lands handed to a LandWorker, a list of handles per repository, held until the next tick collects them.

    A repository holds up to `limit_for(repo)` lands at once. A land waits on checks from the moment its work starts
    until it returns, so `queued_since` lists the start times, in epoch seconds, of the lands running now. A land held
    by a paused gate has not started and has none.
    """

    def __init__(
        self, worker: chair_land.LandWorker, stamp: Callable[[], float] = time.time,
        limit_for: Callable[[str], int] = land_limit.repo_land_limit,
    ) -> None:
        self._worker = worker
        self._stamp = stamp
        self._limit_for = limit_for
        self._handles: dict[str, list[tuple[Action, chair_land.LandHandle[Result]]]] = {}
        self._gate = chair_land.UNPAUSED
        self._since: dict[object, float] = {}
        self._lock = threading.Lock()

    def set_gate(self, gate: CiGate) -> None:
        """Holds later lands while `gate` is paused; an unpaused gate starts the lands it held."""
        self._gate = gate
        self._worker.tick(gate)

    def queued_since(self) -> tuple[float, ...]:
        with self._lock:
            return tuple(self._since.values())

    def landing(self) -> list[dict[str, str]]:
        """One dict per land still in progress, read from the action it was submitted with."""
        return [
            {"initiative": a.get("initiative", ""), "phase": a.get("phase", ""), "repo": a.get("repo", "")}
            for lands in list(self._handles.values())
            for a, h in list(lands)
            if h.in_progress()
        ]

    def _timed(self, work: Callable[[], Result]) -> Callable[[], Result]:
        def timed() -> Result:
            token = object()  # one key per running land, so two lands of one repo keep their own start times
            with self._lock:
                self._since[token] = self._stamp()
            try:
                return work()
            finally:
                with self._lock:
                    self._since.pop(token, None)

        return timed

    def pending(self, repo: str) -> bool:
        return bool(self._handles.get(repo))

    def full(self, repo: str) -> bool:
        return len(self._handles.get(repo, ())) >= self._limit_for(repo)

    def submit(self, action: Action, work: Callable[[], Result]) -> Result:
        repo = action.get("repo", "")
        submitted = self._worker.submit(repo, self._timed(work), self._gate, self._limit_for(repo))
        if submitted.handle is None:
            holder = submitted.outcome.holder if isinstance(submitted.outcome, chair_land.Refused) else None
            reason = land_repo_lease.refusal_message(repo, holder or "another land")
            return {"action": action, "status": "busy", "reason": reason}
        self._handles.setdefault(repo, []).append((action, submitted.handle))
        return {"action": action, "status": "in_progress", "reason": f"landing in {repo}"}

    def stop(self) -> None:
        """Refuses the queued lands and any later one; a land still running is left to finish."""
        self._worker.stop()

    def collect(self) -> list[Result]:
        finished = [
            (repo, entry) for repo, lands in self._handles.items() for entry in lands if not entry[1].in_progress()
        ]
        for repo, entry in finished:
            self._handles[repo].remove(entry)
            if not self._handles[repo]:
                del self._handles[repo]
        return [finished_land(action, handle.result()) for _, (action, handle) in finished]


def land_sink(
    beat: Callable[[], None], clock: Callable[[], float] = time.monotonic, wait: Callable[[float], None] = time.sleep,
    stamp: Callable[[], float] = time.time, released: Callable[[], bool] | None = None,
    limit_for: Callable[[str], int] = land_limit.repo_land_limit,
) -> WorkerLands:
    """Edge. The worker beats the chair lease through `beat` every LAND_BEAT_INTERVAL while any land runs, and honours a takeover's release request through `released`. `limit_for` is how many lands a repo admits at once."""
    return WorkerLands(chair_land.LandWorker(beat, LAND_BEAT_INTERVAL, clock, wait, released), stamp, limit_for)


def _performed(result: Result) -> str:
    """`<kind>:<status>`, then the reason's last line for a refused, failed or not_landed result."""
    text = f"{result['action'].get('kind')}:{result['status']}"
    reason = chair_report.reason_tail(result.get("reason", "")) if result["status"] in ("refused", "failed", "not_landed") else ""
    return f"{text} ({reason})" if reason else text


def error_line(exc: Exception, now: datetime, results: Sequence[Result] = ()) -> str:
    """Eastern time, as `format_status` prints it; results already performed are named so none go unreported."""
    done = ", ".join(_performed(r) for r in results)
    tail = f" | performed: {done}" if done else ""
    return f"chair {now.astimezone(EASTERN):%m-%d %H:%M %Z} | tick error: {type(exc).__name__}: {exc}{tail}"


def as_holder(facts: Facts, force: bool = False) -> Facts:
    """The facts as seen once `take_lease` had won a released or stale lease; a live foreign holder is left alone unless `force`."""
    lease = facts["lease"]
    won = force or lease["released"] or lease["stale"]
    return {**facts, "lease": {**lease, "mine": True}} if won else facts


def _publish_meter(deps: RunDeps, dry_run: bool, now: datetime) -> None:
    """Record the local meter reading as a `meter` action when it is fresh; nothing raised here leaves the tick."""
    if dry_run:
        return
    with contextlib.suppress(Exception):
        doc = deps.meter_doc()
        meter = None if doc is None else usage_meter.parse(doc)
        if meter is None or not usage_meter.fresh(meter, now):
            return
        # `status` is required: the store's record-action refuses a line without ts, epoch, kind and status.
        deps.exec_deps.record({
            "kind": "meter", "status": "recorded",
            "five_hour": doc.get("five_hour"), "seven_day": doc["seven_day"], "observed_at": doc["observed_at"],
        })


def _pull_step(deps: RunDeps, dry_run: bool) -> str:
    """Pull the workspace on a live tick and return the failure reason, "" when it pulled or did not run.

    A failure is recorded as a `pull_workspace` action only when it differs from the last one recorded.
    Nothing raised here leaves the tick: a pull that raises counts as a failure with the exception text.
    """
    if dry_run or deps.pull_workspace is None:
        return ""
    try:
        reason = deps.pull_workspace()
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
    if reason and reason != deps.pull_state.last_reason:
        # `status` is required: the store's record-action refuses a line without ts, epoch, kind and status.
        with contextlib.suppress(Exception):
            deps.exec_deps.record({"kind": "pull_workspace", "status": "recorded", "reason": reason})
            deps.pull_state.last_reason = reason
    elif not reason:
        deps.pull_state.last_reason = ""
    return reason


def _lane_key(action: Action) -> LaneKey:
    return str(action.get("host", "")), str(action.get("cause", ""))


def _named_by_checkout(action: Action) -> Action:
    """The action with its cause suffixed by the checkout its reason names, as `ci_gate_paused: <reason>` does.

    The push key is `needs_chair:<host>:<cause>`, so two dirty checkouts on one host would share a key and the
    second would never be recorded or pushed. The reason reads `lane host H, checkout P: why`.
    """
    head = str(action.get("reason", "")).partition(": ")[0]
    path = head.partition(", checkout ")[2]
    return {**action, "cause": f"{action.get('cause', '')}: {path}"} if path else action


def _lane_checkout_step(deps: RunDeps, dry_run: bool) -> LaneTick:
    """What this tick does with the lane checkout sync; nothing on a dry run or with no sync wired.

    `fresh` leaves out an action whose key is already in `seen`. Nothing here changes `seen`: `_settle_lane_sync`
    does, once `perform` has recorded the action. A sync that raises is one `lane_checkout_error` action, and the
    keys already standing are kept, because a failed sync says nothing about them.
    """
    seen = deps.lane_sync_state.seen
    if dry_run or deps.lane_checkout_actions is None:
        return LaneTick([], [], seen)
    try:
        found = [_named_by_checkout(a) for a in deps.lane_checkout_actions()]
        keys = frozenset(_lane_key(a) for a in found)
    except Exception as exc:
        found = [{
            "kind": "needs_chair", "host": "lane checkouts", "cause": "lane_checkout_error",
            "reason": f"lane checkout sync failed: {str(exc) or type(exc).__name__}",
        }]
        keys = seen | {_lane_key(found[0])}
    return LaneTick([a for a in found if _lane_key(a) not in seen], found, keys)


def _settle_lane_sync(deps: RunDeps, step: LaneTick, results: list[Result]) -> None:
    """Remember a key only if it was already seen or `perform` recorded its action, so a lost one is raised again."""
    recorded = {_lane_key(r["action"]) for r in results if r["status"] == "recorded"}
    deps.lane_sync_state.seen = frozenset(k for k in step.keys if k in deps.lane_sync_state.seen or k in recorded)


def _publish_status(deps: RunDeps, dry_run: bool, line: str, now: datetime, stage_seconds: dict[str, float] | None = None) -> None:
    """Record the tick's status line as a `status` action, so the feed reads it from the store on any machine.

    `stage_seconds` rides on the record when the tick ran to the end; a dry run records nothing.
    """
    if dry_run or not line.strip():
        return
    timing = {"stage_seconds": stage_seconds} if stage_seconds else {}
    with contextlib.suppress(Exception):
        deps.exec_deps.record({"kind": "status", "status": "recorded", "line": line, "at": now.isoformat(), **timing})


def _publish_tick_status(deps: RunDeps, dry_run: bool, line: str, now: datetime) -> None:
    """Write the tick's status JSON to the chair lease row under the held epoch; a False return is not an error."""
    if dry_run or not line.strip() or deps.runs_dir is None:
        return
    with contextlib.suppress(Exception):
        action = deps.read_action(deps.runs_dir)
        text = chair_tick_status.dump_tick_status(chair_tick_status.build_tick_status(line, now, action))
        deps.write_tick_status(deps.runs_dir, deps.current_epoch(), text)


def next_gate_record(was_paused: bool, gate: CiGate, held: tuple[str, ...], epoch: int) -> Action | None:
    """The one needs_chair for a gate that has just paused; the cause carries the reason, as perform records no reason."""
    if was_paused or not gate.paused:
        return None
    named = f"; held for CI: {', '.join(held)}" if held else ""
    return {"kind": "needs_chair", "initiative": "ci-gate", "cause": f"ci_gate_paused: {gate.reason}{named}", "epoch": epoch}


def _evaluate_gate(deps: RunDeps, now: datetime) -> CiGate:
    if deps.status_reader is None:
        return chair_land.UNPAUSED
    return evaluate_gate(deps.status_reader.read(), deps.queued_since(), now.timestamp(), DEFAULT_QUEUED_BOUND_SECONDS)


def _notify_step(deps: RunDeps, dry_run: bool, facts: Facts, actions: list[Action], now: datetime) -> str:
    """Push this tick's needs-you events and pending cards; "" when skipped or sent, a status note on any failure."""
    if dry_run or deps.workspace is None:
        return ""
    try:
        events = [*events_from_tick(facts, actions), *card_events(pending_cards(deps.workspace))]
        deps.notify(events, deps.notify_config, deps.workspace / "notify-sent.json", now.timestamp())
        return ""
    except Exception as exc:  # a failed push must not cost the tick its status line or stop the loop
        return f"notify failed: {type(exc).__name__}: {exc}"


def _gather_bounded(deps: RunDeps, sources: dict[str, float], now: datetime, clock: chair_timing.Clock) -> tuple[Facts, float]:
    """Edge. `chair_timing.timed` around the gather, run in a daemon thread and waited on for at most `deps.facts_bound`.

    A gather that outlives the bound is abandoned, not stopped: its thread is a daemon and its late result is dropped.
    """
    outcome: list[tuple[Facts, float] | BaseException] = []
    done = threading.Event()

    def work() -> None:
        try:
            outcome.append(chair_timing.timed(clock, deps.gather, chair_timing.wrap_callables(deps.facts_deps, clock, sources), now))
        except BaseException as exc:  # carried to the caller's thread, which re-raises it
            outcome.append(exc)
        finally:
            done.set()

    threading.Thread(target=work, name="chair-facts", daemon=True).start()
    start = time.monotonic()
    while not done.wait(max(0.0, deps.facts_bound - (time.monotonic() - start))):
        if overran(time.monotonic() - start, deps.facts_bound):
            raise TickOverrun(deps.facts_bound)
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def tick(deps: RunDeps, dry_run: bool, now: datetime) -> str:
    """The status line of one tick; `tick_timed` holds the rules and the stage seconds."""
    return tick_timed(deps, dry_run, now)[0]


def _release_step(deps: RunDeps, dry_run: bool, now: datetime, clock: chair_timing.Clock) -> str | None:
    """Edge. The status line of a tick that released the chair or stands by after one; None for an ordinary tick.

    A release also sets the beat thread's stop event, so the thread cannot retake the lease just released.
    """
    state = deps.release_state
    if dry_run:
        return None
    stamp = f"chair {now.astimezone(EASTERN):%m-%d %H:%M %Z}"
    if state.released_at is not None:
        left = RELEASE_HOLD_OFF_SECONDS - (clock() - state.released_at)
        if left > 0:
            return f"{stamp} | standby: released at takeover request, holding off {left:.0f}s more"
        state.released_at = None
    if deps.release_requested is None or not deps.release_requested():
        return None
    state.released_at = clock()
    if state.beating is not None:
        state.beating.set()
    return f"{stamp} | released at takeover request"


def tick_timed(
    deps: RunDeps, dry_run: bool, now: datetime, clock: chair_timing.Clock = time.monotonic
) -> tuple[str, dict[str, float | None], dict[str, float]]:
    """Beat first, then gather, plan, perform and format; a failure after perform still names what was performed.

    A live tick first asks `release_requested`; a release, or the hold-off after one, ends the tick before the beat.

    A live tick with `pull_workspace` set pulls right after the beat, so a take_lease has pulled before it runs.
    A failed pull never stops the tick; it adds a note to the line and one action per distinct reason.

    Returns the status line, the seconds of each stage (None for one that did not run) and the seconds of each facts source.
    A dry run never takes the lease, so it plans as the holder would to show the actions a live tick would take.
    The meter is published right after the beat, so a gather, plan or perform that raises cannot skip it.
    A paused CI gate holds lands and dependent relaunches and records one needs_chair when it first pauses. The pause
    is remembered after perform, and only by a tick that acts as the holder, so a tick that fails records it again.
    A live tick that changed rows hands their ids to `export_rows`; its note or failure is appended to the line.
    The unlaunched reason lines of the plan are left on `deps.reason_state`, for a dry run as well.
    """
    deps.reason_state.lines = []
    released = _release_step(deps, dry_run, now, clock)
    if released is not None:
        return released, {}, {}
    sources: dict[str, float] = {}
    deps.beat()
    _publish_meter(deps, dry_run, now)
    pull_failure = _pull_step(deps, dry_run)
    lane = _lane_checkout_step(deps, dry_run)
    gathered, facts_s = _gather_bounded(deps, sources, now, clock)
    facts = as_holder(gathered, deps.force_holder) if dry_run else gathered
    gate = _evaluate_gate(deps, now)
    if not dry_run:
        deps.set_gate(gate)
    if gate.paused:
        planned, plan_s = chair_timing.timed(clock, deps.plan_held, facts, now, gate)
    else:
        plain, plan_s = chair_timing.timed(clock, deps.plan, facts, now)
        planned = TickPlan(plain, ())
    deps.reason_state.lines = chair_report.unlaunched_lines(planned.actions, now)
    acting = not dry_run and all(a.get("kind") != "standby" for a in planned.actions)
    notice = next_gate_record(deps.gate_state.paused, gate, planned.held_for_ci, deps.current_epoch()) if acting and gate.paused else None
    # Fresh lane checkout actions ride the planned list: perform records each needs_chair, and the notify step pushes it.
    # perform fences an action with no epoch, so each carries the current one.
    stamped = [{**a, "epoch": deps.current_epoch()} for a in lane.fresh]
    actions = [*planned.actions, *stamped, *([notice] if notice is not None else [])]
    results, perform_s = chair_timing.timed(clock, deps.perform, actions, deps.exec_deps, deps.current_epoch, dry_run)
    if not dry_run:
        _settle_lane_sync(deps, lane, results)
    # Notify hears every standing lane action each tick: `notify_core.due` pushes a standing key once, and a push
    # that failed is only retried while its event is still presented.
    standing = [a for a in lane.standing if a not in lane.fresh]
    if acting and gate.paused != deps.gate_state.paused:
        deps.gate_state.paused = gate.paused
    ids = () if dry_run or deps.export_rows is None else changed_row_ids(results)
    note, export_s = chair_timing.timed(clock, _export_note, deps.export_rows, ids) if ids and deps.export_rows is not None else ("", None)
    try:
        line = format_status(facts, [*actions, *reported_waiting(facts, now)], results, now)
    except Exception as exc:  # the actions already ran; report them rather than drop them
        line = error_line(exc, now, results)
    line = f"{line} | {chair_exec.NOT_PROBED}" if dry_run else line
    line = f"{line} | {note}" if note else line
    line = f"{line} | workspace pull failed: {pull_failure}" if pull_failure else line
    line = f"{line} | {PROFILE_STALE_NOTE}" if facts.get("profile_stale") else line
    pushed = _notify_step(deps, dry_run, facts, [*actions, *standing], now)
    stages = {"facts": facts_s, "plan": plan_s, "perform": perform_s, "export": export_s}
    return (f"{line} | {pushed}" if pushed else line), stages, sources


def _attempt(deps: RunDeps, dry_run: bool) -> None:
    """One guarded tick and its write; nothing raised here leaves the loop."""
    now = deps.now()
    timing, stage_seconds = None, None
    reasons: list[str] = []
    try:
        line, stages, sources = tick_timed(deps, dry_run, now)
        timing, stage_seconds = chair_timing.format_timing(stages, sources), chair_timing.ran(stages)
        reasons = list(deps.reason_state.lines)
        deps.overrun_state.consecutive = 0
    except TickOverrun as exc:  # a facts stage that never returned; counted, and not an error line
        deps.overrun_state.consecutive += 1
        line = overrun_line(exc.bound, deps.overrun_state.consecutive, now)
    except Exception as exc:  # one bad tick must not stop the loop
        line = error_line(exc, now)
    shown = with_note(line, deps.restart_note)
    _publish_status(deps, dry_run, shown, now, stage_seconds)
    _publish_tick_status(deps, dry_run, shown, now)
    try:
        if timing is not None:
            # Before the status line: `_chair_run` takes its --once exit code from the last line echoed.
            with contextlib.suppress(Exception):
                deps.report_deps.echo(timing)
        for reason in reasons:  # same rule: the status line stays last
            with contextlib.suppress(Exception):
                deps.report_deps.echo(reason)
        write_status(shown, deps.report_deps)
    except Exception as exc:  # a failed notify or echo must not stop the loop either
        # A broken echo leaves nowhere to write, so the loop goes on silent rather than dying.
        with contextlib.suppress(Exception):
            deps.report_deps.echo(error_line(exc, now))


class LeaseRejected(Exception):
    """The lease code refused a beat: a newer epoch or another holder owns the lease."""


def _fenced(beat: Callable[[], object]) -> Callable[[], None]:
    """A refusal line from the lease code becomes a raise, so the beat thread stops on it."""

    def call() -> None:
        refusal = beat()
        if refusal:
            raise LeaseRejected(str(refusal))

    return call


def start_lease_beat(deps: RunDeps, dry_run: bool) -> threading.Event:
    """Edge. Starts the daemon beat thread unless this is a dry run or no beat is wired, and returns its stop event.

    A rejected or raising beat sets the event, so the thread stops; `gather` then reads the lost lease on the next tick.
    """
    stop = threading.Event()
    deps.release_state.beating = stop  # a takeover release sets it, so the thread cannot retake the lease
    if deps.lease_beat is not None and not dry_run:
        beat_in_thread(_fenced(deps.lease_beat), deps.lease_beat_interval, stop, lambda _exc: stop.set())
    return stop


def _shutdown(deps: RunDeps, beating: threading.Event, restore: Callable[[], None], dry_run: bool) -> None:
    beating.set()  # before the release: a beat with no sidecar acquires, and would retake the lease just released
    restore()  # a second SIGTERM during the release below ends the process as it would have
    with contextlib.suppress(Exception):  # a failed land release must not keep the chair lease held
        deps.stop_lands()
    if deps.save_cache is not None and not dry_run:
        with contextlib.suppress(Exception):  # a failed cache save must not keep the chair lease held
            deps.save_cache()
    if deps.holds():
        deps.release()


def _moved_heads(deps: RunDeps, pending: Pending | None) -> tuple[tuple[str, str] | None, str, Pending | None]:
    """(heads, reason, pending): heads is set once a code move has settled and no land is in flight; None to tick on.

    A land in flight or an unreadable checkout keeps `pending`. A head back where it started, or a move that touches no chair code, drops it.
    """
    started = deps.started_checkout
    if deps.checkout_state is None or started is None:
        return None, "", None
    try:
        current = deps.checkout_state()
    except Exception:  # an unreadable checkout is no reason to restart
        return None, "", pending
    state = restart_decision(started, current, deps.lands_in_flight()).state
    if current is None or state == WAIT:
        return None, "", pending
    if state != RESTART:
        return None, "", None
    paths = None
    if deps.changed_files is not None:
        try:
            paths = deps.changed_files(started.head, current.head)
        except Exception:  # an unreadable diff counts as relevant
            paths = None
        if paths is not None and not touches_chair_code(paths):
            return None, "", None
    now = deps.monotonic()
    seen = pending or Pending(current.head, now, now)
    ready, kept = coalesce(seen, current.head, now, deps.restart_window_s, deps.restart_max_defer_s)
    moved = (started.head, current.head)
    return (moved, restart_reason(*moved, paths), None) if ready else (None, "", kept)


def _restart(deps: RunDeps, moved: tuple[str, str], reason: str) -> None:
    with contextlib.suppress(Exception):  # a broken echo must not keep the chair lease held
        deps.report_deps.echo(restart_line(*moved))
    deps.restart_state.moved = moved
    deps.restart_state.reason = reason
    if deps.record_restart is not None:
        with contextlib.suppress(Exception):  # a failed record must not keep the chair lease held
            deps.record_restart(reason)


def run(once: bool, interval: float, dry_run: bool, deps: RunDeps, max_overruns: int = 3) -> None:
    """Ticks until interrupted, SIGTERM, max_overruns overruns in a row or a moved checkout, or once; a stop frees the lands, then the lease.

    The checkout is read between ticks only, so a tick in progress finishes first; a land in flight defers the restart.
    A move that touches no chair code never restarts; a burst of code moves restarts once, after the head settles.
    """
    restore = (lambda: None) if dry_run else deps.install_sigterm()
    beating = start_lease_beat(deps, dry_run)
    pending: Pending | None = None  # carried across passes: the code move waiting for its head to settle
    try:
        while True:
            _attempt(deps, dry_run)
            if once:
                return
            if overrun_limit_hit(deps.overrun_state.consecutive, max_overruns):
                _shutdown(deps, beating, restore, dry_run)
                return
            if deps.wait is None:
                deps.sleep(interval)
            else:
                deps.wait(interval)
            if not dry_run:
                moved, reason, pending = _moved_heads(deps, pending)
                if moved is not None:
                    _restart(deps, moved, reason)
                    _shutdown(deps, beating, restore, dry_run)
                    return
    except KeyboardInterrupt:
        _shutdown(deps, beating, restore, dry_run)
    finally:
        beating.set()
        restore()
