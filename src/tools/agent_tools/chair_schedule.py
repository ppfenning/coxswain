"""Pure core: graph, estimates, hosts and a clock value in; one Schedule out; no clock, file or process."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agent_tools.chair_schedule_durations import Estimate
from agent_tools.chair_schedule_graph import Key, Node
from agent_tools.chair_schedule_path import critical_paths

SCHEDULE_FILE = "schedule.json"  # the document name the publisher writes and the feed reads
MIN_REMAINING_SECONDS = 900.0
ORCHESTRATING = "orchestrating"  # the chair machine builds nothing; any other mode lets `local` hosts build
SCHEMA = 1


@dataclass(frozen=True)
class Host:
    name: str
    lanes: tuple[str, ...]  # one entry per lane; the tuple length is the host's lane capacity
    local: bool = False
    repos: tuple[str, ...] = ()  # empty: capable of every repo


@dataclass(frozen=True)
class Running:
    host: str
    lane: str
    finish: datetime  # estimated; it may already be past


@dataclass(frozen=True)
class Placement:
    key: Key
    host: str
    lane: str
    start: datetime
    finish: datetime
    critical: bool


@dataclass(frozen=True)
class LanePlan:
    host: str
    lane: str
    next: tuple[Placement, ...]


@dataclass(frozen=True)
class InitiativePlan:
    initiative: str
    priority: int
    projected_finish: datetime | None
    on_critical_path: bool


@dataclass(frozen=True)
class Tie:
    """Nodes ready together with equal critical value and equal priority; only queue order separated them."""

    keys: tuple[Key, ...]  # in queue order


@dataclass(frozen=True)
class Explanation:
    """Why a node sits where it does.

    `binding` is now, lane, parent or running for a node with a place. For one without, it is
    blocked or waiting (both from status), no_estimate (no duration given) or unplaced (no capable host, or
    something it waits on has none).
    """

    key: Key
    value: float
    order: int | None  # 0-based pick order; None for a running or unplaced node
    host: str | None
    lane: str | None
    host_load: int | None  # live load plus earlier placements on that host, as seen at the pick
    binding: str


@dataclass(frozen=True)
class Schedule:
    launches: tuple[Placement, ...]
    lanes: tuple[LanePlan, ...]
    initiatives: tuple[InitiativePlan, ...]
    release_finish: datetime | None
    target: datetime | None
    misses_target: bool | None
    critical_path: tuple[Key, ...]
    ties: tuple[Tie, ...]
    explanations: tuple[Explanation, ...]
    unplaced: tuple[Key, ...] = ()
    cycle: tuple[Key, ...] = ()


def remaining_seconds(finish: datetime | None, now: datetime) -> float:
    """Seconds a running node still holds its lane; an overdue or unrecorded one holds it for the minimum."""
    left = (finish - now).total_seconds() if finish is not None else 0.0
    return left if left > 0 else MIN_REMAINING_SECONDS


def _seconds(node: Node, durations: Mapping[Key, Estimate], running: Mapping[Key, Running], now: datetime) -> float:
    if node.status == "running":
        record = running.get(node.key)
        return remaining_seconds(record.finish if record else None, now)
    estimate = durations.get(node.key)
    return estimate.seconds if estimate else 0.0


def _capable(host: Host, node: Node, mode: str) -> bool:
    return (
        bool(host.lanes) and (not host.repos or node.repo in host.repos) and not (mode == ORCHESTRATING and host.local)
    )


def _weight(key: Key, values: Mapping[Key, float], nodes: Mapping[Key, Node]) -> tuple[float, int]:
    """Lower sorts first: the larger critical value, then the higher initiative priority."""
    return (-values[key], -nodes[key].priority)


def _binding(start: datetime, now: datetime, parents_done: datetime) -> str:
    """What set the start: nothing (now), the last parent (also when a lane frees at the same moment), or the lane."""
    if start == now:
        return "now"
    return "parent" if parents_done == start else "lane"


def _parents_done(
    key: Key, parents: Mapping[Key, tuple[Key, ...]], finish: Mapping[Key, datetime], now: datetime
) -> datetime:
    """When the last parent finishes in the projection; every parent must already be placed."""
    return max((finish[p] for p in parents[key]), default=now)


def _earliest(
    host: Host, index: int, free: Mapping[tuple[str, str], datetime], t: datetime
) -> tuple[datetime, int, int]:
    """(start, host index, lane index) of the host's first lane to free up, no earlier than t."""
    start, lane_index = min((max(free[(host.name, ln)], t), li) for li, ln in enumerate(host.lanes))
    return (start, index, lane_index)


def _misses(release: datetime | None, target: datetime | None, partial: bool) -> bool | None:
    """None without a target. True once the placed work alone passes it. None when unplaced work leaves it unknown."""
    if target is None or release is None:
        return None
    if release > target:
        return True
    return None if partial else False


def plan_schedule(
    nodes: Mapping[Key, Node],
    dependents: Mapping[Key, frozenset[Key]],
    durations: Mapping[Key, Estimate],
    hosts: Sequence[Host],
    mode: str,
    load: Mapping[str, int],
    now: datetime,
    running: Mapping[Key, Running] | None = None,
    target: datetime | None = None,
) -> Schedule:
    """List-schedule every open node; `dependents` maps a key to the keys that wait on it, as `build_graph` returns.

    The projection clock starts at `now` and jumps to the next moment a placed node's parents finish, so a node is
    picked only when it is ready then, and an idle host takes independent work instead of reserving a far-future slot.
    Among hosts of equal load the one whose lane frees first wins, then host order.

    A running node holds its lane and its children for its remaining time, or for `MIN_REMAINING_SECONDS` once it
    is overdue or has no record; its finish is never clamped to `now`.
    A blocked or waiting node, or one with no entry in `durations`, has no projectable start: it is left unplaced,
    and so is every node that waits on it. `release_finish` then covers placed work only, a lower bound, and
    `misses_target` is True when that bound passes the target and None, not False, when it does not.
    """
    held = running or {}
    seconds = {k: _seconds(n, durations, held, now) for k, n in nodes.items()}
    path = critical_paths(seconds, dependents)
    if path.cycle:
        return Schedule((), (), (), None, target, None, (), (), (), cycle=path.cycle)
    queue = {k: i for i, k in enumerate(nodes)}
    parents = {k: tuple(p for p, after in dependents.items() if k in after) for k in nodes}
    chain = frozenset(path.chain)
    finish = {k: now + timedelta(seconds=seconds[k]) for k, n in nodes.items() if n.status == "running"}
    free = {
        (h.name, lane): max(
            (finish[k] for k, r in held.items() if k in finish and (r.host, r.lane) == (h.name, lane)), default=now
        )
        for h in hosts
        for lane in h.lanes
    }
    placements: list[Placement] = []
    why: list[Explanation] = []
    ties: list[Tie] = []
    placed_on = {h.name: 0 for h in hosts}
    excluded = {
        k: n.status if n.status in ("blocked", "waiting") else "no_estimate"
        for k, n in nodes.items()
        if n.status != "running" and (n.status in ("blocked", "waiting") or k not in durations)
    }
    pending = [k for k, n in nodes.items() if n.status != "running" and k not in excluded]
    # List scheduling is sequential by nature: the mutation below stays inside this loop.
    t = now  # the projection clock: a node is ready only once its parents have finished by t
    while placed := [k for k in pending if all(p in finish for p in parents[k])]:
        ready = [k for k in placed if _parents_done(k, parents, finish, now) <= t]
        if not ready:
            t = min(_parents_done(k, parents, finish, now) for k in placed)
            continue
        best = min(ready, key=lambda k: (*_weight(k, path.values, nodes), queue[k]))
        group = tuple(
            sorted(
                (k for k in ready if _weight(k, path.values, nodes) == _weight(best, path.values, nodes)), key=queue.get
            )
        )
        if len(group) > 1 and not any(set(group) <= set(t.keys) for t in ties):
            ties.append(Tie(group))
        pending.remove(best)
        options = [
            (load.get(h.name, 0) + placed_on[h.name], *_earliest(h, i, free, t))
            for i, h in enumerate(hosts)
            if _capable(h, nodes[best], mode)
        ]
        if not options:
            continue
        host_load, start, host_index, lane_index = min(options)
        host = hosts[host_index]
        lane = host.lanes[lane_index]
        done = _parents_done(best, parents, finish, now)
        finish[best] = start + timedelta(seconds=seconds[best])
        free[(host.name, lane)] = finish[best]
        placed_on[host.name] += 1
        placements.append(Placement(best, host.name, lane, start, finish[best], best in chain))
        why.append(
            Explanation(
                best,
                path.values[best],
                len(placements) - 1,
                host.name,
                lane,
                host_load,
                _binding(start, now, done),
            )
        )
    unplaced = tuple(k for k in nodes if k not in finish)
    return Schedule(
        launches=tuple(p for p in placements if p.start == now),
        lanes=_lane_plans(hosts, placements),
        initiatives=_initiatives(nodes, finish, chain),
        release_finish=(release := max(finish.values(), default=None)),
        target=target,
        misses_target=_misses(release, target, bool(unplaced)),
        critical_path=path.chain,
        ties=tuple(ties),
        explanations=tuple(why) + _other_explanations(nodes, held, path.values, unplaced, excluded),
        unplaced=unplaced,
    )


def _other_explanations(
    nodes: Mapping[Key, Node],
    held: Mapping[Key, Running],
    values: Mapping[Key, float],
    unplaced: tuple[Key, ...],
    excluded: Mapping[Key, str],
) -> tuple[Explanation, ...]:
    on_lane = tuple(
        Explanation(
            k,
            values[k],
            None,
            held[k].host if k in held else None,
            held[k].lane if k in held else None,
            None,
            "running",
        )
        for k, n in nodes.items()
        if n.status == "running"
    )
    return on_lane + tuple(
        Explanation(k, values[k], None, None, None, None, excluded.get(k, "unplaced")) for k in unplaced
    )


def _lane_plans(hosts: Sequence[Host], placements: Sequence[Placement]) -> tuple[LanePlan, ...]:
    plans = (
        LanePlan(
            h.name,
            lane,
            tuple(sorted((p for p in placements if (p.host, p.lane) == (h.name, lane)), key=lambda p: p.start)),
        )
        for h in hosts
        for lane in h.lanes
    )
    return tuple(p for p in plans if p.next)


def _initiatives(
    nodes: Mapping[Key, Node], finish: Mapping[Key, datetime], chain: frozenset[Key]
) -> tuple[InitiativePlan, ...]:
    ids = dict.fromkeys(n.initiative for n in nodes.values())
    return tuple(
        InitiativePlan(
            i,
            next(n.priority for n in nodes.values() if n.initiative == i),
            max((finish[k] for k, n in nodes.items() if n.initiative == i and k in finish), default=None),
            any(k in chain for k, n in nodes.items() if n.initiative == i),
        )
        for i in ids
    )


def _iso(moment: datetime) -> str:
    utc = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else _iso(moment)


def _span(late: timedelta) -> str:
    hours, minutes = divmod(round(late.total_seconds() / 60), 60)
    return f"{hours}h{minutes}m" if hours and minutes else f"{hours}h" if hours else f"{minutes}m"


def _advice(schedule: Schedule, at: datetime) -> list[dict]:
    if not schedule.misses_target or schedule.release_finish is None or schedule.target is None:
        return []
    last = schedule.critical_path[-1:] if schedule.critical_path else ()
    through = "".join(f"; the critical path runs through {i} {p}" for i, p in last)
    text = f"Release projected {_span(schedule.release_finish - schedule.target)} after target{through}."
    return [{"kind": "miss", "text": text, "at": _iso(at)}]


def _body(schedule: Schedule, at: datetime) -> dict:
    return {
        "lanes": [
            {
                "host": lane.host,
                "lane": lane.lane,
                "next": [
                    {
                        "initiative": p.key[0],
                        "phase": p.key[1],
                        "start": _iso(p.start),
                        "finish": _iso(p.finish),
                        "critical": p.critical,
                    }
                    for p in lane.next
                ],
            }
            for lane in schedule.lanes
        ],
        "initiatives": [
            {
                "initiative": i.initiative,
                "priority": i.priority,
                "projected_finish": _iso_or_none(i.projected_finish),
                "on_critical_path": i.on_critical_path,
            }
            for i in schedule.initiatives
        ],
        "release": {
            "target": _iso_or_none(schedule.target),
            "projected_finish": _iso_or_none(schedule.release_finish),
            "misses_target": schedule.misses_target,
        },
        "critical_path": [{"initiative": i, "phase": p} for i, p in schedule.critical_path],
        "advice": _advice(schedule, at),
    }


def schedule_updated_action(schedule: Schedule, at: datetime) -> dict:
    """The `schedule_updated` action: the kind and the schedule body."""
    return {"kind": "schedule_updated", "schedule": _body(schedule, at)}


def schedule_feed(schedule: Schedule, at: datetime) -> dict:
    """The feed form of `dash_feed_schedule_v1.json`: schema, `at`, the schedule body and its action."""
    return {
        "schema": SCHEMA,
        "at": _iso(at),
        "schedule": _body(schedule, at),
        "action": schedule_updated_action(schedule, at),
    }
