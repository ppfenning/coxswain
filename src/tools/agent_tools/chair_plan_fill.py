"""Plan the free lanes: started epics, then unstarted ones, then intake decomposes, then a pull.

Pure. free_lanes comes from the caller; only host_free_slots reads dispatch and the optional limits lane_fraction.
"""
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from typing import NamedTuple

from agent_tools import chair_login_watch, host_load
from agent_tools.chair_plan_recover import budget_verdict
from agent_tools.chair_schedule import Schedule
from agent_tools.chair_steer import steer_check
from agent_tools.chair_types import Action, Facts, InitiativeFacts, RunningInitiative, SteerPolicy
from agent_tools.checkout_env import blocks_launch, host_env_verdict, sync_instruction

# name, weight (a host's share of new placements; its capacity when the hosts table sets none),
# capabilities, free lane count, assigned-so-far (live lanes plus placements made this call)
HostSlot = tuple[str, int, frozenset[str], int, int]


class HostEnv(NamedTuple):
    """host_env: host name to its versions record; reported: `host|repo` keys a stale_checkout already named."""

    records: Mapping[str, Mapping]
    reported: frozenset[str]


_NO_ENV = HostEnv({}, frozenset())

# Seconds: the minimum gap between pulls.
PULL_MIN_AGE_S = 600


def _required_capabilities(initiative: InitiativeFacts) -> frozenset[str]:
    return frozenset(cap for task in initiative["ready_tasks"] for cap in task.get("requires", []))


def _launchable(initiative: InitiativeFacts, held_phases: frozenset[tuple[str, str]]) -> InitiativeFacts:
    """The initiative with held-phase ready tasks dropped; ready_surfaces stays, so steer overlap still sees them."""
    if not held_phases:
        return initiative
    kept = [t for t in initiative["ready_tasks"] if (initiative["id"], t["phase"]) not in held_phases]
    return {**initiative, "ready_tasks": kept}


def _eligible(host: HostSlot, required: frozenset[str]) -> bool:
    _, _, capabilities, free, _ = host
    return free > 0 and required <= capabilities


def _rank(pair: tuple[int, HostSlot]) -> tuple[float, int, int]:
    """Share is (live + placed + 1) / weight, then heavier first, then list order: host order never skews the weighting."""
    idx, (_, weight, _, _, assigned) = pair
    return ((assigned + 1) / weight, -weight, idx)


def best_host(
    required: frozenset[str], hosts: Sequence[HostSlot], barred: Collection[str] = ()
) -> tuple[str, list[HostSlot]] | None:
    """The best-ranked unbarred host whose capabilities cover required, and the hosts after one placement on it."""
    candidates = [(idx, host) for idx, host in enumerate(hosts) if _eligible(host, required) and host[0] not in barred]
    if not candidates:
        return None
    idx, (name, weight, capabilities, free, assigned) = min(candidates, key=_rank)
    updated: HostSlot = (name, weight, capabilities, free - 1, assigned + 1)
    return name, [updated if i == idx else host for i, host in enumerate(hosts)]


_best_host = best_host  # chair_plan.py still imports this name; it is outside the launch-placement change


def _epic_action(initiative_id: str, host: str) -> Action:
    return {"kind": "launch_epic", "initiative": initiative_id, "host": host}


def _decompose_action(intake_id: str, host: str) -> Action:
    return {"kind": "launch_decompose", "intake_ids": [intake_id], "host": host}


def _place_on_hosts(
    rest: list[tuple[str, frozenset[str]]],
    hosts: Sequence[HostSlot],
    make: Callable[[str, str], Action] = _epic_action,
) -> list[Action]:
    """Each item goes to the best-ranked host whose capabilities cover its requires; none eligible, none placed."""
    if not rest:
        return []
    item_id, required = rest[0]
    placed = best_host(required, hosts)
    if placed is None:
        return _place_on_hosts(rest[1:], hosts, make)
    name, new_hosts = placed
    return [make(item_id, name), *_place_on_hosts(rest[1:], new_hosts, make)]


def _after_placing(hosts: Sequence[HostSlot], actions: Sequence[Action]) -> list[HostSlot]:
    """hosts with one free lane taken, and one assignment counted, for each hosted action."""
    taken = [a["host"] for a in actions if "host" in a]
    return [
        (name, weight, capabilities, free - taken.count(name), assigned + taken.count(name))
        for name, weight, capabilities, free, assigned in hosts
    ]


def _barred_hosts(initiative: InitiativeFacts, hosts: Sequence[HostSlot], env: HostEnv) -> dict[str, str]:
    """Host name to a blocking verdict for the initiative's repo_dir, for each host with a free lane."""
    repo_dir = initiative.get("repo_dir", "")
    verdicts = {name: host_env_verdict(dict(env.records.get(name, {})), repo_dir) for name, *_, free, _ in hosts if free > 0}
    return {name: verdict for name, verdict in verdicts.items() if blocks_launch(verdict)}


def _stale_needs_chair(
    initiative: InitiativeFacts,
    barred: Mapping[str, str],
    hosts: Sequence[HostSlot],
    required: frozenset[str],
    env: HostEnv,
) -> list[Action]:
    """One stale_checkout needs_chair per barred host that could have taken the initiative, unless already reported."""
    repo_dir = initiative.get("repo_dir", "")
    return [
        {
            "kind": "needs_chair",
            "initiative": initiative["id"],
            "cause": "stale_checkout",
            "reason": f"{sync_instruction(name, repo_dir)} ({verdict})",
        }
        for name, _, capabilities, *_ in hosts
        if (verdict := barred.get(name)) is not None and required <= capabilities and f"{name}|{repo_dir}" not in env.reported
    ]


def _spent(launch_budget: int | None) -> int | None:
    return None if launch_budget is None else launch_budget - 1


def _walk_candidates(
    candidates: Sequence[InitiativeFacts],
    running: Sequence[RunningInitiative],
    streaks: Mapping[str, int],
    local_left: int,
    hosts: Sequence[HostSlot],
    env: HostEnv = _NO_ENV,
    policy: SteerPolicy | None = None,
    launch_budget: int | None = None,
) -> tuple[list[Action], list[Action]]:
    """Launches and steer actions in walk order; only a launch that takes a lane uses it and claims its surfaces.

    A hosted placement skips a host whose checkout is stale for the initiative's repo_dir; the second list then
    carries a stale_checkout needs_chair for it, once per call per initiative and not yet in env.reported.

    launch_budget bounds the launches returned; the walk stops when it is spent, so a candidate past the cut
    leaves no steer hold and no claim."""
    if not candidates or (local_left <= 0 and not any(free for *_, free, _ in hosts)):
        return [], []
    if launch_budget is not None and launch_budget <= 0:
        return [], []
    head, *tail = candidates
    required = _required_capabilities(head)
    barred = {} if local_left > 0 else _barred_hosts(head, hosts, env)
    notes = _stale_needs_chair(head, barred, hosts, required, env)
    placed = None if local_left > 0 else best_host(required, hosts, barred)
    if local_left <= 0 and placed is None:
        launches, steers = _walk_candidates(tail, running, streaks, local_left, hosts, env, policy, launch_budget)
        return launches, [*notes, *steers]
    repo = head.get("repo", "")
    surfaces = head.get("ready_surfaces", [])
    steer = steer_check(head["id"], repo, surfaces, running, streaks, policy=policy) if repo and surfaces else None
    if steer is not None:
        launches, steers = _walk_candidates(tail, running, streaks, local_left, hosts, env, policy, launch_budget)
        return launches, [*notes, steer, *steers]
    claim: list[RunningInitiative] = [{"id": head["id"], "repo": repo, "surfaces": surfaces}] if repo and surfaces else []
    if placed is None:
        local: Action = {"kind": "launch_epic", "initiative": head["id"]}
        launches, steers = _walk_candidates(
            tail, [*running, *claim], streaks, local_left - 1, hosts, env, policy, _spent(launch_budget)
        )
        return [local, *launches], steers
    name, next_hosts = placed
    hosted: Action = {"kind": "launch_epic", "initiative": head["id"], "host": name}
    launches, steers = _walk_candidates(
        tail, [*running, *claim], streaks, local_left, next_hosts, env, policy, _spent(launch_budget)
    )
    return [hosted, *launches], [*notes, *steers]


def _once_per_reason(actions: Sequence[Action]) -> list[Action]:
    """The first action for each distinct reason: several initiatives skipping one host name it once."""
    return [a for n, a in enumerate(actions) if a["reason"] not in {b["reason"] for b in actions[:n]}]


def budget_skips(facts: Facts, initiatives: Sequence[InitiativeFacts], now: datetime | None) -> dict[str, str]:
    """Initiative id to the budget reason that leaves it out of this tick's launch_epic list; empty when now is None."""
    if now is None:
        return {}
    verdicts = {i["id"]: budget_verdict(facts, i["id"], "launch_epic", now) for i in initiatives}
    return {initiative: v.reason for initiative, v in verdicts.items() if not v.allowed}


def _epic_launches(
    facts: Facts,
    free_lanes: int,
    withheld: frozenset[str],
    host_free: Sequence[HostSlot],
    claimed: Sequence[RunningInitiative] = (),
    now: datetime | None = None,
    held_phases: frozenset[tuple[str, str]] = frozenset(),
    launch_budget: int | None = None,
) -> list[Action]:
    """Started initiatives with ready tasks, then unstarted ones, each in docket order; withheld ids never launch.

    launch_budget, when not None, is the most launches returned, local and hosted together, in this order.

    A ready task in a held (initiative, phase) is not launchable; an initiative left with none is left out.

    An initiative with a `dag_problem` never launches either; `plan_recover` is the one place that reports it.

    With now, an initiative the launch budget refuses is left out like a withheld one; `budget_skips` names why.

    The first free_lanes launch locally. The rest fill host_free in proportion to weight, capped at each
    host's free count, and only onto a host whose capabilities cover the initiative's ready-task requires.
    One that overlaps a running, claimed or launched initiative in its repo yields its lane; steer actions follow.
    """
    candidates = [
        i
        for i in (_launchable(i, held_phases) for i in facts["initiatives"])
        if i["ready_tasks"] and i["id"] not in withheld and not i.get("dag_problem")
    ]
    skips = budget_skips(facts, candidates, now)
    open_ = [i for i in candidates if i["id"] not in skips]
    ordered = [*(i for i in open_ if i["started"]), *(i for i in open_ if not i["started"])]
    running = [*facts.get("running", []), *claimed]
    env = HostEnv(facts.get("host_env", {}), frozenset(facts.get("env_stale_reported", [])))
    launches, steers = _walk_candidates(
        ordered, running, facts.get("steer_streaks", {}), max(0, free_lanes), host_free, env, facts.get("steer_policy"),
        launch_budget,
    )
    stale = [a for a in steers if a.get("cause") == "stale_checkout"]
    return [*launches, *(a for a in steers if a not in stale), *_once_per_reason(stale)]


def _scheduled_walk(
    picks: Sequence[tuple[str, str | None]],
    by_id: Mapping[str, InitiativeFacts],
    running: Sequence[RunningInitiative],
    streaks: Mapping[str, int],
    local_left: int,
    hosts: Sequence[HostSlot],
    env: HostEnv,
    policy: SteerPolicy | None,
    launch_budget: int | None = None,
) -> tuple[list[Action], list[Action]]:
    """Launches and steer actions for the scheduled (initiative, host) picks, in schedule order.

    A pick with no lane, a barred host or a steer hold is dropped; nothing is picked in its place.
    The walk stops once launch_budget launches are placed."""
    if not picks:
        return [], []
    if launch_budget is not None and launch_budget <= 0:
        return [], []
    (initiative_id, host), *tail = picks
    head = by_id[initiative_id]
    required = _required_capabilities(head)
    slot = next((h for h in hosts if h[0] == host), None)
    barred = {} if host is None or slot is None else _barred_hosts(head, [slot], env)
    notes = [] if slot is None else _stale_needs_chair(head, barred, [slot], required, env)
    fits = local_left > 0 if host is None else slot is not None and _eligible(slot, required) and host not in barred
    if not fits:
        launches, steers = _scheduled_walk(tail, by_id, running, streaks, local_left, hosts, env, policy, launch_budget)
        return launches, [*notes, *steers]
    repo = head.get("repo", "")
    surfaces = head.get("ready_surfaces", [])
    steer = steer_check(initiative_id, repo, surfaces, running, streaks, policy=policy) if repo and surfaces else None
    if steer is not None:
        launches, steers = _scheduled_walk(tail, by_id, running, streaks, local_left, hosts, env, policy, launch_budget)
        return launches, [steer, *steers]
    claim: list[RunningInitiative] = [{"id": initiative_id, "repo": repo, "surfaces": surfaces}] if repo and surfaces else []
    action: Action = {"kind": "launch_epic", "initiative": initiative_id, **({} if host is None else {"host": host})}
    launches, steers = _scheduled_walk(
        tail, by_id, [*running, *claim], streaks, local_left - (host is None), _after_placing(hosts, [action]), env, policy,
        _spent(launch_budget),
    )
    return [action, *launches], steers


def scheduled_launches(
    facts: Facts,
    schedule: Schedule,
    free_lanes: int,
    withheld: frozenset[str],
    host_free: Sequence[HostSlot],
    claimed: Sequence[RunningInitiative] = (),
    now: datetime | None = None,
    held_phases: frozenset[tuple[str, str]] = frozenset(),
    launch_budget: int | None = None,
) -> list[Action]:
    """The schedule's this-tick launches as launch_epic actions, in schedule order and on the scheduled host.

    A placement on a local host in `schedule_inputs` carries no host. Only the first placement of an initiative
    counts. The gates of `_epic_launches` apply, and a launch one withholds is dropped, never replaced.
    launch_budget, when not None, is the most launches returned, local and hosted together."""
    local = {h["name"] for h in facts["schedule_inputs"]["hosts"] if h["local"]}
    open_ = [
        i
        for i in (_launchable(i, held_phases) for i in facts["initiatives"])
        if i["ready_tasks"] and i["id"] not in withheld and not i.get("dag_problem")
    ]
    skips = budget_skips(facts, open_, now)
    by_id = {i["id"]: i for i in open_ if i["id"] not in skips}
    wanted = [(p.key[0], None if p.host in local else p.host) for p in schedule.launches if p.key[0] in by_id]
    picks = [pick for n, pick in enumerate(wanted) if pick[0] not in {w[0] for w in wanted[:n]}]
    env = HostEnv(facts.get("host_env", {}), frozenset(facts.get("env_stale_reported", [])))
    launches, steers = _scheduled_walk(
        picks, by_id, [*facts.get("running", []), *claimed], facts.get("steer_streaks", {}), max(0, free_lanes),
        host_free, env, facts.get("steer_policy"), launch_budget,
    )
    stale = [a for a in steers if a.get("cause") == "stale_checkout"]
    return [*launches, *(a for a in steers if a not in stale), *_once_per_reason(stale)]


def _decompose_launches(
    facts: Facts,
    lanes: int,
    host_free: Sequence[HostSlot] = (),
    stalled: frozenset[str] = frozenset(),
    launch_budget: int | None = None,
) -> list[Action]:
    """One intake item per free local lane, oldest first; the rest go to lane hosts, ranked as epics are.

    A decompose requires no capabilities, so any host with a free lane is eligible, and each hosted one counts
    toward its host's weighted share for the epics placed after it. A stalled intake is never launched.
    launch_budget, when not None, keeps only the first that many, local ones before hosted ones."""
    fit = max(0, lanes)
    open_intake = [i for i in facts["intake"] if i not in stalled]
    local: list[Action] = [{"kind": "launch_decompose", "intake_ids": [i]} for i in open_intake[:fit]]
    overflow = [(intake_id, frozenset[str]()) for intake_id in open_intake[fit:]]
    launches = [*local, *_place_on_hosts(overflow, host_free, _decompose_action)]
    return launches if launch_budget is None else launches[: max(0, launch_budget)]


def _wants_pull(facts: Facts, lanes_left: int) -> bool:
    has_ready = any(i["ready_tasks"] for i in facts["initiatives"])
    return (
        lanes_left > 0
        and not facts["work_store_ready"]
        and facts["sources_configured"]
        and not has_ready
        and not facts["intake"]
        and (facts.get("pull_age_s") is None or facts["pull_age_s"] >= PULL_MIN_AGE_S)
    )


def _scaled_lanes(capacity: int, lane_fraction: float) -> int:
    """chair_facts.scaled_cap's rule, kept local because chair_facts imports chair_plan, which imports this module."""
    return 0 if lane_fraction <= 0 else max(1, int(capacity * lane_fraction))


def host_free_slots(facts: Facts) -> list[HostSlot]:
    dispatch = facts["dispatch"]
    rows = facts.get("login_hosts", [])
    blocked = chair_login_watch.login_blocked(rows) | host_load.overloaded(rows, facts.get("host_load_ceiling")).keys()
    fraction = facts.get("limits", {}).get("lane_fraction", 1.0)
    return [
        (
            h["name"],
            h["weight"] if "weight" in h else max(1, h.get("capacity", dispatch["max_in_flight"])),
            frozenset(h.get("capabilities", [])),
            0
            if h["name"] in blocked
            else max(0, _scaled_lanes(h.get("capacity", dispatch["max_in_flight"]), fraction) - h["live_runs"]),
            h["live_runs"],
        )
        for h in dispatch["hosts"]
    ]


def _less_consumed(host_free: list[HostSlot], consumed_host_lanes: dict[str, int]) -> list[HostSlot]:
    """host_free with each host's free count reduced by the lanes a prior planning step already took."""
    return [
        (name, weight, capabilities, max(0, free - consumed_host_lanes.get(name, 0)), assigned)
        for name, weight, capabilities, free, assigned in host_free
    ]


def plan_fill(
    facts: Facts,
    free_lanes: int,
    withheld: frozenset[str] = frozenset(),
    consumed_host_lanes: dict[str, int] | None = None,
    claimed: Sequence[RunningInitiative] = (),
    hosted_decompose: bool = False,
    stalled: frozenset[str] = frozenset(),
    now: datetime | None = None,
    schedule: Schedule | None = None,
    *,
    held_phases: frozenset[tuple[str, str]] = frozenset(),
    launch_budget: int | None = None,
) -> list[Action]:
    """launch_budget, when not None, is the most launch_epic and launch_decompose actions returned in total,
    local and hosted together; epics rank ahead of decomposes. None leaves the count to the lanes.

    now enables the launch budget: an initiative it refuses gets no launch_epic. Without it the budget is skipped.

    schedule, when given, replaces the ranking and host choice: only its this-tick launches become launch_epic.

    held_phases names (initiative, phase) pairs whose ready tasks are skipped when picking; an initiative with
    only held ready work is left out like a withheld one. Held tasks still count for steer overlap.

    claimed names initiatives launched earlier this tick; they count as running for the overlap check.

    consumed_host_lanes names lane-host slots a recovery step already placed a relaunch or retry on this
    tick, so fill never places a fresh launch_epic on a lane that action just filled.

    dispatch local_lanes "decompose" keeps the free local lanes for intake decomposes: every epic goes to a
    lane host, and the decomposes take the local lanes.

    hosted_decompose sends a decompose past the local lanes to a lane host, before epics are placed on hosts.
    It defaults off: `cox route launch decompose` has no `--on` yet, so a hosted decompose would fail to parse.

    stalled names intake paths at the empty-decompose limit. They stay in facts["intake"], so no pull is planned,
    but no launch_decompose is built for them."""
    host_free = host_free_slots(facts)
    if consumed_host_lanes:
        host_free = _less_consumed(host_free, consumed_host_lanes)
    if free_lanes <= 0 and not any(free for *_, free, _ in host_free):
        return []
    local_lanes = max(0, free_lanes)
    reserved = facts["dispatch"].get("local_lanes") == "decompose"
    epic_lanes = 0 if reserved else free_lanes

    def epics_on(hosts: Sequence[HostSlot], budget: int | None) -> list[Action]:
        if schedule is None:
            return _epic_launches(facts, epic_lanes, withheld, hosts, claimed, now, held_phases, budget)
        return scheduled_launches(facts, schedule, epic_lanes, withheld, hosts, claimed, now, held_phases, budget)

    first_pass = epics_on(host_free, launch_budget)
    first_epics = [a for a in first_pass if a["kind"] == "launch_epic"]
    local_epics = [a for a in first_epics if "host" not in a]
    decompose_budget = None if launch_budget is None else launch_budget - len(first_epics)
    decomposes = _decompose_launches(
        facts, local_lanes - len(local_epics), host_free if hosted_decompose else (), stalled, decompose_budget
    )
    # Host lanes a decompose took are gone before epics are placed on hosts; the local epic count is unchanged.
    # The second pass keeps at least the first pass's epic count of budget, so no first-pass epic is cut.
    epic_budget = None if launch_budget is None else launch_budget - len(decomposes)
    epics = epics_on(_after_placing(host_free, decomposes), epic_budget)
    local_decomposes = [a for a in decomposes if "host" not in a]
    lanes_left = local_lanes - len(local_epics) - len(local_decomposes)
    pulls: list[Action] = [{"kind": "pull"}] if _wants_pull(facts, lanes_left) else []
    return [*epics, *decomposes, *pulls]
