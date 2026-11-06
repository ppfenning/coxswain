"""Pure scheduler: the whole queue's graph packed onto host lanes, and the projected schedule.

Plain data in, a `Schedule` out. The caller passes `now`; nothing here reads a clock, file or process.
The feed and the publisher both name the document `SCHEDULE_FILE`.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from agent_tools.chair_schedule_graph import Key, Node

SCHEDULE_FILE = "schedule.json"
ORCHESTRATING = "orchestrating"  # the chair machine plans and does not build; any other mode builds anywhere
MIN_REMAINING_SECONDS = 300.0  # an overdue running node still holds its lane this long past `now`


@dataclass(frozen=True)
class HostSlot:
    """A host's lanes in preference order. Empty `repos` means any repo."""

    host: str
    lanes: tuple[str, ...]
    repos: frozenset[str] = frozenset()
    chair: bool = False


@dataclass(frozen=True)
class Running:
    """Where a running node sits and when it is estimated to finish; its record, not this estimate, ends it."""

    host: str
    lane: str
    finish: datetime


_NO_RUNNING: Mapping[Key, Running] = MappingProxyType({})


@dataclass(frozen=True)
class Placement:
    key: Key
    host: str
    lane: str
    start: datetime
    finish: datetime
    critical: bool
    running: bool = False


@dataclass(frozen=True)
class LaneRow:
    host: str
    lane: str
    next: tuple[Placement, ...]


@dataclass(frozen=True)
class InitiativeRow:
    initiative: str
    priority: int
    projected_finish: datetime
    on_critical_path: bool


@dataclass(frozen=True)
class Release:
    target: datetime | None
    projected_finish: datetime | None
    misses_target: bool | None


@dataclass(frozen=True)
class Explain:
    """Why a node sits where it does. `waited_on` is `running`, `parent`, `lane` or `none`."""

    key: Key
    critical_value: float
    priority: int
    host: str
    lane: str
    host_load: int
    ready_at: datetime
    waited_on: str


@dataclass(frozen=True)
class Schedule:
    """`ties` groups ready nodes equal on critical value and priority, in the order queue position broke them."""

    launches: tuple[Placement, ...]
    lanes: tuple[LaneRow, ...]
    initiatives: tuple[InitiativeRow, ...]
    release: Release
    critical_path: tuple[Key, ...]
    ties: tuple[tuple[Key, ...], ...]
    explain: tuple[Explain, ...]
    unplaced: tuple[Key, ...] = ()


def _parents(nodes: Mapping[Key, Node], dependents: Mapping[Key, frozenset[Key]]) -> dict[Key, frozenset[Key]]:
    return {k: frozenset(p for p, after in dependents.items() if k in after and p in nodes) for k in nodes}


def _pin(run: Running, now: datetime) -> datetime:
    return max(run.finish, now + timedelta(seconds=MIN_REMAINING_SECONDS))


def schedule(
    nodes: Mapping[Key, Node],
    dependents: Mapping[Key, frozenset[Key]],
    durations: Mapping[Key, float],
    values: Mapping[Key, float],
    chain: Sequence[Key],
    hosts: Sequence[HostSlot],
    mode: str,
    load: Mapping[str, int],
    now: datetime,
    running: Mapping[Key, Running] = _NO_RUNNING,
    target: datetime | None = None,
) -> Schedule:
    """List scheduling: the ready node with the highest critical value goes first, then priority, then queue order.

    A node goes to the capable host with the lowest live load plus placements made here, on that host's
    earliest free lane. Seconds in `durations` and `values` must cover every node key.
    """
    # List scheduling is a loop over a worklist; the mutation below stays inside this function.
    parents = _parents(nodes, dependents)
    queue = {k: i for i, k in enumerate(nodes)}
    on_chain = frozenset(chain)
    usable = [h for h in hosts if not (mode == ORCHESTRATING and h.chair)]
    free = {(h.host, lane): now for h in usable for lane in h.lanes}
    placed: dict[Key, Placement] = {}
    explain: list[Explain] = []
    ties: list[tuple[Key, ...]] = []
    stuck: set[Key] = set()
    made: Counter[str] = Counter()

    for key, run in running.items():
        if key in nodes:
            finish = _pin(run, now)
            free[(run.host, run.lane)] = finish
            placed[key] = Placement(key, run.host, run.lane, now, finish, key in on_chain, running=True)
            explain.append(
                Explain(
                    key,
                    values.get(key, 0),
                    nodes[key].priority,
                    run.host,
                    run.lane,
                    load.get(run.host, 0),
                    now,
                    "running",
                )
            )

    while True:
        ready = [k for k in nodes if k not in placed and k not in stuck and parents[k].issubset(placed)]
        if not ready:
            break
        weight = {k: (values.get(k, 0), nodes[k].priority) for k in ready}
        order = sorted(ready, key=lambda k: (-weight[k][0], -weight[k][1], queue[k]))
        pick = order[0]
        tied = tuple(k for k in order if weight[k] == weight[pick])
        if len(tied) > 1 and tied not in ties:
            ties.append(tied)
        ready_at = max((placed[p].finish for p in parents[pick]), default=now)
        ready_at = max(ready_at, now)
        options = [
            (h, min(h.lanes, key=lambda lane: free[(h.host, lane)]))
            for h in usable
            if h.lanes and (not h.repos or nodes[pick].repo in h.repos)
        ]
        if not options:
            stuck.add(pick)
            continue
        host, lane = min(
            options,
            key=lambda o: (load.get(o[0].host, 0) + made[o[0].host], max(free[(o[0].host, o[1])], ready_at)),
        )
        lane_free = free[(host.host, lane)]
        start = max(lane_free, ready_at)
        finish = start + timedelta(seconds=durations[pick])
        waited = "lane" if lane_free > ready_at else ("parent" if ready_at > now else "none")
        explain.append(
            Explain(
                pick,
                weight[pick][0],
                weight[pick][1],
                host.host,
                lane,
                load.get(host.host, 0) + made[host.host],
                ready_at,
                waited,
            )
        )
        free[(host.host, lane)] = finish
        made[host.host] += 1
        placed[pick] = Placement(pick, host.host, lane, start, finish, pick in on_chain)

    placements = sorted(placed.values(), key=lambda p: p.start)
    lanes = tuple(
        LaneRow(h.host, lane, rows)
        for h in hosts
        for lane in h.lanes
        if (rows := tuple(p for p in placements if (p.host, p.lane) == (h.host, lane)))
    )
    initiatives = tuple(
        InitiativeRow(
            ini,
            next(n.priority for n in nodes.values() if n.initiative == ini),
            max(p.finish for p in mine),
            any(p.critical for p in mine),
        )
        for ini in dict.fromkeys(n.initiative for n in nodes.values())
        if (mine := [p for p in placements if p.key[0] == ini])
    )
    last = max((p.finish for p in placements), default=None)
    release = Release(target, last, None if target is None or last is None else last > target)
    return Schedule(
        launches=tuple(p for p in placements if p.start == now and not p.running),
        lanes=lanes,
        initiatives=initiatives,
        release=release,
        critical_path=tuple(chain),
        ties=tuple(ties),
        explain=tuple(explain),
        unplaced=tuple(sorted(k for k in nodes if k not in placed)),
    )


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _placement_feed(p: Placement) -> dict:
    return {
        "initiative": p.key[0],
        "phase": p.key[1],
        "start": _iso(p.start),
        "finish": _iso(p.finish),
        "critical": p.critical,
    }


def schedule_body(plan: Schedule, advice: Sequence[Mapping] = ()) -> dict:
    """The `schedule` object of the feed. Launches, ties and explain data stay on the `Schedule` and are not fed."""
    return {
        "lanes": [{"host": r.host, "lane": r.lane, "next": [_placement_feed(p) for p in r.next]} for r in plan.lanes],
        "initiatives": [
            {
                "initiative": i.initiative,
                "priority": i.priority,
                "projected_finish": _iso(i.projected_finish),
                "on_critical_path": i.on_critical_path,
            }
            for i in plan.initiatives
        ],
        "release": {
            "target": _iso(plan.release.target),
            "projected_finish": _iso(plan.release.projected_finish),
            "misses_target": plan.release.misses_target,
        },
        "critical_path": [{"initiative": i, "phase": p} for i, p in plan.critical_path],
        "advice": [dict(a) for a in advice],
    }


def schedule_updated_action(body: Mapping) -> dict:
    return {"kind": "schedule_updated", "schedule": dict(body)}


def to_feed(plan: Schedule, at: datetime, advice: Sequence[Mapping] = ()) -> dict:
    """The document named `SCHEDULE_FILE`: the schedule and the `schedule_updated` action that carries it."""
    body = schedule_body(plan, advice)
    return {"schema": 1, "at": _iso(at), "schedule": body, "action": schedule_updated_action(body)}
