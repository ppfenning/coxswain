"""Pure: whether an intake's decompose is still in flight, so no worker launches a second one. No file, process or clock access.

A queued intake names no initiative until its decompose writes rows, so nothing else in the facts says a decompose of
it has started. A local decompose runs to the end inside the launching process, which hid the gap; a decompose placed
on a lane host returns at once, and the launch worker plans again on the next `runs` notification, which that very run
sends. One decompose per intake is in flight when any of these hold, whatever the worker or placement:

- the newest launch of it the action log recorded done is younger than `SETTLE_S` (the run has not written its `runs`
  row yet: the ssh, the copy and the start take seconds);
- a counted run of it has no end yet and was launched within `RUN_MAX_S` (past that the decompose timeout has killed it);
- its newest counted run ended within `SETTLE_S` (its rows are written after the run ends, and a cached queue read can
  trail them by the store's counter flush);
- its newest counted run ended having written task items: the intake is decomposed whatever a stale queue read says.

A run or launch before `since` (the intake's last release or requeue) is not counted, as `chair_decompose_streak.counted`."""
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime

from agent_tools.chair_decompose_streak import DecomposeRun, counted, missed

SETTLE_S = 300.0  # past the ssh, copy and start of a hosted run, and past the store's counter flush on a busy server
RUN_MAX_S = 7200.0  # twice `cli_route_launch._DECOMPOSE_TIMEOUT_S`: an unended run older than this is not in flight
QUEUED_AFTER_ROWS_CAUSE = "decompose_queued_after_rows"  # needs_chair: a run wrote items, its intake row stayed queued


def _age_s(then: datetime, now: datetime) -> float:
    """Seconds from `then` to `now`; a `then` in the future (a host clock ahead of the chair's) is age 0, never "old"."""
    return max(0.0, (now - then).total_seconds())


def in_flight_reason(
    runs: Sequence[DecomposeRun],
    launches: Sequence[datetime],
    since: datetime | None,
    now: datetime,
    lost: Collection[str] = frozenset(),
) -> str | None:
    """Why the intake must not launch another decompose now, or None when it may. `runs` is oldest first; `launches`
    are the times the action log recorded a done launch_decompose of the intake, in any order. A run id in `lost`
    (the `lost_runs` fact: its host went quiet or its lease expired with no end) no longer holds the intake."""
    kept = [r for r in runs if counted(r, since)]
    live = [
        r for r in kept
        if not r.exited and r.run_id not in lost and r.launched_at is not None and _age_s(r.launched_at, now) < RUN_MAX_S
    ]
    if live:
        return f"its decompose run {live[-1].run_id} has not ended"
    recent = [t for t in launches if (since is None or t >= since) and _age_s(t, now) < SETTLE_S]
    if recent:
        return f"a decompose launch was recorded {int(_age_s(max(recent), now))}s ago and its run has not reported yet"
    ended = [r for r in kept if r.exited]
    if not ended:
        return None
    newest = ended[-1]
    if not missed(newest):
        return f"its decompose run {newest.run_id} wrote task items"
    if newest.ended_at is not None and _age_s(newest.ended_at, now) < SETTLE_S:
        return f"its decompose run {newest.run_id} ended {int(_age_s(newest.ended_at, now))}s ago"
    return None


def queued_after_rows(runs: Sequence[DecomposeRun], since: datetime | None, now: datetime) -> str | None:
    """The run id when the intake's newest counted run ended more than `SETTLE_S` ago having written task items, so
    the intake, still queued, is held for good (`in_flight_reason`) and a person must look. None otherwise. A run with
    no readable end counts as long ended. Only reported; nothing marks the intake."""
    kept = [r for r in runs if counted(r, since)]
    if not kept or not kept[-1].exited or missed(kept[-1]):
        return None
    newest = kept[-1]
    settled = newest.ended_at is None or _age_s(newest.ended_at, now) >= SETTLE_S
    return newest.run_id if settled else None


def launch_times(docs: Sequence[Mapping[str, object]]) -> dict[str, list[datetime]]:
    """Intake path to the times of its done launch_decompose rows, from action docs; a row with no readable time is left out."""
    from agent_tools.chair_decompose_runs import (
        parse_when,  # lazy: chair_decompose_runs imports chair_facts, which imports this
    )

    found: dict[str, list[datetime]] = {}
    for doc in docs:
        paths = doc.get("intake_ids")
        when = parse_when(doc.get("ts"))
        if doc.get("kind") != "launch_decompose" or doc.get("status") != "done" or when is None:
            continue
        if isinstance(paths, (list, tuple)) and paths and isinstance(paths[0], str):
            found.setdefault(paths[0], []).append(when)
    return found


def in_flight_facts(
    paths: Sequence[str],
    runs: Mapping[str, Sequence[DecomposeRun]],
    launches: Mapping[str, Sequence[datetime]],
    since: Mapping[str, datetime | None],
    now: datetime,
    lost: Collection[str] = frozenset(),
) -> dict[str, str]:
    """Intake path to why its decompose is in flight, for only the paths that are."""
    reasons = {p: in_flight_reason(runs.get(p, ()), launches.get(p, ()), since.get(p), now, lost) for p in paths}
    return {p: reason for p, reason in reasons.items() if reason is not None}


def queued_after_rows_facts(
    paths: Sequence[str], runs: Mapping[str, Sequence[DecomposeRun]], since: Mapping[str, datetime | None], now: datetime
) -> dict[str, str]:
    """Intake path to the run that wrote its items while the intake stayed queued, for only those paths."""
    found = {p: queued_after_rows(runs.get(p, ()), since.get(p), now) for p in paths}
    return {p: run for p, run in found.items() if run is not None}


def queued_after_rows_keys(docs: Sequence[Mapping[str, object]]) -> list[str]:
    """`<intake>|<run>` of each decompose_queued_after_rows needs_chair already in the action log, in row order."""
    return [
        f"{d['intake']}|{d['run']}"
        for d in docs
        if d.get("kind") == "needs_chair" and d.get("cause") == QUEUED_AFTER_ROWS_CAUSE and d.get("intake") and d.get("run")
    ]
