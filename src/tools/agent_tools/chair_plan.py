"""Compose one chair tick: the lease first, then lands, recovery, staleness and fill under the limits gate.

Pure. Takes the facts and the tick's clock, returns actions each stamped with the lease epoch. No I/O.
"""
import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent_tools import (
    chair_launch_why,
    chair_lease_refusal,
    chair_login_watch,
    chair_plan_prune,
    chair_stall,
    chair_steward,
)
from agent_tools.chair_carry import plan_carry
from agent_tools.chair_idle_stall import diagnose, is_stalled
from agent_tools.chair_plan_fill import (
    HostSlot,
    _best_host,
    _eligible,
    _less_consumed,
    _place_on_hosts,
    _required_capabilities,
    budget_skips,
    host_free_slots,
    plan_fill,
)
from agent_tools.chair_plan_land import backoff_held, fetch_action, newest_run, plan_finish, plan_lands
from agent_tools.chair_plan_recover import (
    _initiative_first_unmet_need,
    claimed_by,
    plan_lost_runs,
    plan_recover,
    refused_launches,
)
from agent_tools.chair_plan_review import plan_review
from agent_tools.chair_plan_routine import routine_only
from agent_tools.chair_plan_stale import plan_stale
from agent_tools.chair_plan_tune import plan_tune
from agent_tools.chair_plan_widen import plan_widen
from agent_tools.chair_rebase import plan_rebase
from agent_tools.chair_revert import revert_check
from agent_tools.chair_runnable import Blocking, TaskView, WaitingInitiative, runnable_verdict
from agent_tools.chair_steer import steer_heads
from agent_tools.chair_types import (
    DEADLOCK_CAUSE,
    HELD_FILE_CAUSE,
    Action,
    ApprovedTask,
    DispatchFacts,
    Facts,
    IdleStallInputs,
    InitiativeFacts,
    LastCall,
    LeaseFacts,
    LimitsFacts,
    StallCandidate,
    stamp,
)
from agent_tools.ci_gate import CiGate
from core.landing import DEFAULT_LAND_AT

_UNPAUSED = CiGate(False, "")
_LAUNCHES = {"relaunch", "retry", "rescue"}
_DEAD_PID_SILENCE = timedelta(minutes=10)


def _launch_cap(limits: LimitsFacts) -> int:
    return 0 if limits["go_degraded"] or limits.get("smoke_hold") is not None else max(limits["launch_cap"], 0)


def initiative_homes(newest_run_host: Mapping[str, str], unfinished: Collection[str], local_host: str = "") -> dict[str, str]:
    """An initiative in `unfinished` (a carried partial phase or an approved task not yet landed) is homed on
    its newest run's host, "" meaning this machine; an initiative with no unfinished work has no entry.
    A run recorded under `local_host`, the chair's own name, is this machine too, so it homes on ""."""
    def here(host: str) -> str:
        return "" if local_host and host == local_host else host
    return {initiative: here(newest_run_host[initiative]) for initiative in unfinished if initiative in newest_run_host}


def _lease_gate(lease: LeaseFacts) -> list[Action] | None:
    """None when the lease is mine and the tick goes on; otherwise the one action the tick returns."""
    if lease["mine"]:
        return None
    if lease["released"] or lease["stale"]:
        return [{"kind": "take_lease"}]
    if lease.get("expired", False):
        return [{"kind": "take_lease", "reason": f"takeover expired at {lease.get('until', '')}"}]
    until = lease.get("until", "")
    return [{"kind": "standby", "holder": lease["holder"], "host": lease["host"], **({"until": until} if until else {})}]


def _route_homed(
    actions: list[Action], launch_at: list[int], home: Mapping[str, str], cap: int, host_free: Sequence[HostSlot]
) -> tuple[dict[int, str], set[int], int, dict[str, int]]:
    """Route each homed relaunch, retry or rescue straight to home[initiative]: "" claims one of the cap local
    slots, a named host claims one of its free lanes by name alone, no ranking and no capability check. No
    free slot there drops the index. Returns host-by-index for the ones placed on a host, the dropped indices,
    the count of local slots this used, and the per-host lane counts consumed."""
    host_left = {name: free for name, _, _, free, _ in host_free}
    homed_at = [n for n in launch_at if actions[n]["initiative"] in home]
    host_for_index: dict[int, str] = {}
    dropped: set[int] = set()
    local_used = 0
    for n in homed_at:
        destination = home[actions[n]["initiative"]]
        if destination == "":
            if local_used < cap:
                local_used += 1
            else:
                dropped.add(n)
        elif host_left.get(destination, 0) > 0:
            host_left[destination] -= 1
            host_for_index[n] = destination
        else:
            dropped.add(n)
    consumed: dict[str, int] = {}
    for host in host_for_index.values():
        consumed[host] = consumed.get(host, 0) + 1
    return host_for_index, dropped, local_used, consumed


def _drained_hosts(login_hosts: Sequence[Mapping]) -> frozenset[str]:
    """Names of rows whose state is 'draining': out of service, so a home there stays stuck past this tick,
    unlike a merely busy `active` host whose lanes will free up on their own."""
    return frozenset(str(row.get("name") or "") for row in login_hosts if row.get("state") == "draining")


def _unreachable_hosts(login_hosts: Sequence[Mapping]) -> frozenset[str]:
    """Names of rows whose state is 'unreachable': a dead machine, so a home there is stuck."""
    return frozenset(str(row.get("name") or "") for row in login_hosts if row.get("state") == chair_login_watch.UNREACHABLE_STATE)


def _stuck_homes(actions: list[Action], launch_at: list[int], home: Mapping[str, str], facts: Facts) -> set[int]:
    """Indices whose home host is drained, unreachable, login-lapsed, or whose lane is lost: these relocate instead
    of routing home. While local lanes are decompose-only, a relaunch or retry homed on "" is stuck too."""
    login_hosts = facts.get("login_hosts", [])
    stuck_hosts = chair_login_watch.login_blocked(login_hosts) | _drained_hosts(login_hosts) | _unreachable_hosts(login_hosts)
    lost = frozenset(facts.get("lost_runs", {}))
    decompose_only = facts.get("dispatch", {}).get("local_lanes") == "decompose"
    return {
        n
        for n in launch_at
        if actions[n]["initiative"] in home
        and (
            home[actions[n]["initiative"]] in stuck_hosts
            or actions[n]["initiative"] in lost
            or (decompose_only and home[actions[n]["initiative"]] == "" and actions[n]["kind"] in ("relaunch", "retry"))
        )
    }


def _home_fetch(initiative: str, approved: list[ApprovedTask]) -> Action | None:
    """plan_lands' fetch for the initiative's newest run among its approved rows, the run its home was placed
    by; None when no row names a run, so there is nothing to fetch the move from."""
    rows = [t for t in approved if t["initiative"] == initiative and t["run"]]
    if not rows:
        return None
    run = newest_run({t["run"] for t in rows})
    return fetch_action(run, next(t["repo"] for t in rows if t["run"] == run), initiative)


def _freest_machine(
    required: frozenset[str], last: str | None, pinned: bool, local_free: int, hosts: Sequence[HostSlot]
) -> str | None:
    """The machine with the most free lanes, "" meaning this one, or None when none has a free lane.

    A host lacking `required` is not a candidate; this machine always is. A tie goes to `last`, then to this
    machine, then to the host _best_host ranks first among the tied hosts. `pinned` leaves only `last`."""
    capable = [host for host in hosts if _eligible(host, required)]
    free = {"": local_free, **{host[0]: host[3] for host in capable}}
    options = {name: lanes for name, lanes in free.items() if lanes > 0 and (not pinned or name == last)}
    if not options:
        return None
    tied = [name for name, lanes in options.items() if lanes == max(options.values())]
    if last in tied:
        return last
    if "" in tied:
        return ""
    best = _best_host(required, [host for host in capable if host[0] in tied])
    return best[0] if best is not None else None


def _place_freest(
    wanting: Sequence[tuple[int, frozenset[str], str | None, bool]], local_free: int, hosts: Sequence[HostSlot]
) -> dict[int, str]:
    """Index to machine ("" this one) for each (index, required, last host, pinned) in order. Each placement
    lowers that machine's free count for the next; one with no machine free is left out."""
    if not wanting:
        return {}
    (n, required, last, pinned), *rest = wanting
    machine = _freest_machine(required, last, pinned, local_free, hosts)
    if machine is None:
        return _place_freest(rest, local_free, hosts)
    if machine == "":
        return {n: "", **_place_freest(rest, local_free - 1, hosts)}
    taken = [
        (name, weight, caps, free - 1, assigned + 1) if name == machine else (name, weight, caps, free, assigned)
        for name, weight, caps, free, assigned in hosts
    ]
    return {n: machine, **_place_freest(rest, local_free, taken)}


def _cap_launches(
    actions: list[Action], cap: int, initiatives: list[InitiativeFacts], host_free: Sequence[HostSlot], home: Mapping[str, str],
    facts: Facts,
) -> tuple[list[Action], dict[str, int]]:
    """A relaunch or retry goes to the machine with the most free lanes, this one included unless its local lanes are
    decompose-only (_place_freest): a
    tie goes to the initiative's home, then to this machine, then to the host _best_host ranks first. A host
    lacking the initiative's capabilities is skipped. A placement here carries no host, one elsewhere carries
    host, and a paired clear_branches always runs locally. A move off a remote home plans a fetch of its newest
    run's branches first; with no run to fetch, the initiative stays pinned to its home. A rescue keeps the old
    rule: routed to its home (see _route_homed) or, homeless, one of the cap local slots, dropped past them.

    A relaunch or retry whose home host is drained, login-lapsed, or whose lane is lost, or a "" home while local
    lanes are decompose-only, is relocated instead:
    ranked by _place_on_hosts, its own old home host excluded from the candidates, never a local lane, with a
    fetch for its newest run's branches planned right before its own first action; with no run to fetch it is
    dropped and a needs_chair names it instead. What nothing can take is dropped. A dropped relaunch takes its
    paired clear_branches with it.

    Returns the resulting actions and a mapping of host name to the lanes this placement took, for the fill
    step that follows to subtract.
    """
    launch_at = [n for n, a in enumerate(actions) if a["kind"] in _LAUNCHES]
    stuck = _stuck_homes(actions, launch_at, home, facts)
    stuck_initiatives = {actions[n]["initiative"] for n in stuck}
    freest_at = [n for n in launch_at if actions[n]["kind"] in ("relaunch", "retry") and n not in stuck]
    freest_initiatives = {actions[n]["initiative"] for n in freest_at}
    rescue_at = [n for n in launch_at if n not in freest_at]
    routed_home = {i: h for i, h in home.items() if i not in stuck_initiatives and i not in freest_initiatives}
    homed_host_for_index, homed_dropped, local_used, homed_consumed = _route_homed(actions, rescue_at, routed_home, cap, host_free)
    unhomed_at = [n for n in rescue_at if actions[n]["initiative"] not in routed_home and n not in stuck]
    overflow = unhomed_at[max(0, cap - local_used) :]
    movable = {actions[n]["initiative"] for n in stuck if actions[n]["kind"] in ("relaunch", "retry")}
    fetches = {i: _home_fetch(i, facts["approved"]) for i in movable}
    unfetchable = {i for i, fetch in fetches.items() if fetch is None}
    stuck_hostable = [n for n in stuck if actions[n]["initiative"] in movable - unfetchable and actions[n]["kind"] != "rescue"]
    by_id = {i["id"]: i for i in initiatives}

    def required_for(n: int) -> frozenset[str]:
        initiative = actions[n]["initiative"]
        return _required_capabilities(by_id[initiative]) if initiative in by_id else frozenset()

    ordered = stuck_hostable
    rest = [(actions[n]["initiative"], required_for(n)) for n in ordered]
    excluded = {home[actions[n]["initiative"]] for n in stuck if home.get(actions[n]["initiative"])}
    reduced_host_free = [
        (name, weight, capabilities, max(0, free - homed_consumed.get(name, 0)), assigned)
        for name, weight, capabilities, free, assigned in host_free
        if name not in excluded
    ]
    placed = iter(_place_on_hosts(rest, reduced_host_free))
    next_placed = next(placed, None)
    host_for_index: dict[int, str] = dict(homed_host_for_index)
    for n in ordered:
        if next_placed is not None and next_placed["initiative"] == actions[n]["initiative"]:
            host_for_index[n] = next_placed["host"]
            next_placed = next(placed, None)
    taken_so_far: dict[str, int] = {}
    for host in host_for_index.values():
        taken_so_far[host] = taken_so_far.get(host, 0) + 1
    # A move off a remote home needs a fetch of its branches; with none to fetch the initiative stays pinned there.
    free_fetches = {i: _home_fetch(i, facts["approved"]) for i in freest_initiatives if home.get(i)}
    # Decompose-only local lanes take no relaunch or retry: the freest machine is then always a lane host (2026-10-06,
    # chair-widens and each-release were placed on the decompose-only Mac).
    decompose_only = facts.get("dispatch", {}).get("local_lanes") == "decompose"
    local_left = 0 if decompose_only else max(0, cap - local_used - min(len(unhomed_at), max(0, cap - local_used)))
    freest = _place_freest(
        [
            (
                n,
                required_for(n),
                home.get(actions[n]["initiative"]),
                bool(home.get(actions[n]["initiative"])) and free_fetches[actions[n]["initiative"]] is None,
            )
            for n in freest_at
        ],
        local_left,
        _less_consumed(host_free, taken_so_far),
    )
    host_for_index = {**host_for_index, **{n: machine for n, machine in freest.items() if machine}}
    consumed: dict[str, int] = {}
    for host in host_for_index.values():
        consumed[host] = consumed.get(host, 0) + 1
    dropped = (
        homed_dropped
        | {n for n in stuck if n not in host_for_index}
        | {n for n in overflow if n not in host_for_index}
        | {n for n in freest_at if n not in freest}
    )
    dropped_initiatives = {actions[n]["initiative"] for n in dropped if actions[n]["kind"] == "relaunch"}
    kept_at = [
        n
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in dropped_initiatives)
    ]
    moved_off_home = {
        actions[n]["initiative"]
        for n, machine in freest.items()
        if home.get(actions[n]["initiative"]) and machine != home[actions[n]["initiative"]]
    }
    relocated = {actions[n]["initiative"] for n in stuck_hostable if n in host_for_index} | moved_off_home
    # The fetch goes ahead of the moved initiative's first kept action, a mark_lost or its clear_branches.
    fetch_before = {
        min(n for n in kept_at if actions[n].get("initiative") == i): fetch
        for i, fetch in {**fetches, **free_fetches}.items()
        if i in relocated and fetch is not None
    }
    kept = [
        action
        for n in kept_at
        for action in (
            *([fetch_before[n]] if n in fetch_before else []),
            _hosted(actions[n], host_for_index.get(n), routed_home),
        )
    ]
    # A stuck home with no run to fetch from never moves blind: the chair hears why the relaunch was dropped.
    unmoved: list[Action] = [{"kind": "needs_chair", "initiative": i, "cause": "home_unfetchable"} for i in sorted(unfetchable)]
    return [*kept, *unmoved], consumed


def _needs_chair_only(actions: list[Action]) -> list[Action]:
    return [a for a in actions if a["kind"] == "needs_chair"]


def _without_carried_lands(lands: list[Action], carries: list[Action]) -> list[Action]:
    """A phase planned for carry is not also landed this tick, by phase or by task."""
    carried = {(a["initiative"], a["phase"]) for a in carries if a["kind"] == "carry_phase"}
    return [a for a in lands if not (a["kind"] in ("land_phase", "partial_land") and (a["initiative"], a["phase"]) in carried)]


def _landing_initiatives(facts: Facts, lands: list[Action]) -> frozenset[str]:
    """Initiatives whose repo has a land in flight, plus those with a land_phase or partial_land planned this tick."""
    repos = frozenset(landing["repo"] for landing in facts.get("landing", []))
    in_repo = frozenset(i["id"] for i in facts["initiatives"] if i.get("repo") in repos)
    return in_repo | frozenset(a["initiative"] for a in lands if a["kind"] in ("land_phase", "partial_land"))


def _is_launch(action: Action) -> bool:
    return action["kind"] in _LAUNCHES or action["kind"] == "launch_epic"


def _with_rebases(actions: list[Action], rebases: list[Action]) -> list[Action]:
    """Each rebase_phase directly before the first relaunch, retry, rescue or launch_epic of its initiative.

    A rebase whose launch the cap dropped has no anchor and leads the list: it starts no run, so the cap does not bind it."""
    first: dict[str, int] = {}
    for n in reversed(range(len(actions))):
        if _is_launch(actions[n]):
            first[actions[n]["initiative"]] = n
    unanchored = [r for r in rebases if r["initiative"] not in first]
    placed = [x for n, a in enumerate(actions) for x in (*(r for r in rebases if first.get(r["initiative"]) == n), a)]
    return [*unanchored, *placed]


def _hosted(action: Action, placed: str | None, routed_home: Mapping[str, str]) -> Action:
    """A launch gains the host it was placed on. A clear_branches gains its initiative's home host, so its carry
    runs where the relaunch will; a local home or a relocated initiative leaves it without one."""
    if placed is not None:
        return {**action, "host": placed}
    home = routed_home.get(action.get("initiative", ""), "")
    return {**action, "host": home} if action["kind"] == "clear_branches" and home else action


def _with_carry(action: Action, approved: list[dict]) -> Action:
    """A clear_branches gains `carry`, the sorted phases of the initiative still partial, when there are any."""
    if action["kind"] != "clear_branches":
        return action
    carry = sorted(chair_plan_prune.phases_to_carry(approved, action["initiative"]))
    return {**action, "carry": carry} if carry else action


def _dispatch_room(dispatch: DispatchFacts) -> int:
    """The lanes this machine has free before any launch this tick."""
    return max(0, dispatch["max_in_flight"] - dispatch["live_runs"])


def _free_lanes(cap: int, kept: int, dispatch: DispatchFacts) -> int:
    return max(0, min(cap - kept, dispatch["max_in_flight"] - dispatch["live_runs"] - kept))


def _fetch_exit_actions(facts: Facts) -> list[Action]:
    """One fetch_exit per remote-unfetched initiative, naming its stranded run."""
    return [{"kind": "fetch_exit", "initiative": initiative, "run": run} for initiative, run in facts.get("remote_unfetched", {}).items()]


def _withhold_remote_unfetched(actions: list[Action], remote_unfetched: frozenset[str]) -> list[Action]:
    """Drop a relaunch for a remote-unfetched initiative; a dropped relaunch takes its paired clear_branches with it."""
    dropped = {n for n, a in enumerate(actions) if a["kind"] == "relaunch" and a["initiative"] in remote_unfetched}
    return [
        a
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in remote_unfetched)
    ]


_HOST_BOUND_KINDS = frozenset({"fetch", "clear_branches", "land", "land_phase", "partial_land"})


def _withhold_blocked_hosts(actions: list[Action], facts: Facts) -> list[Action]:
    """Drop a fetch, clear_branches or land whose run or initiative is homed on a login-blocked host.

    The run is looked up in run_hosts first, then the initiative in initiative_homes. needs_chair and every other
    kind pass; the filter reads only these facts, so a host whose login_ok turns true plans again that same tick.
    """
    blocked = chair_login_watch.login_blocked(facts.get("login_hosts", []))
    run_hosts = facts.get("run_hosts", {})
    homes = facts.get("initiative_homes", {})
    return [
        a
        for a in actions
        if not (
            blocked
            and a["kind"] in _HOST_BOUND_KINDS
            and (run_hosts.get(a.get("run", "")) or homes.get(a.get("initiative", ""))) in blocked
        )
    ]


def _withhold_not_exited(actions: list[Action], not_exited: frozenset[str]) -> list[Action]:
    """Drop a relaunch for an initiative whose newest run has no recorded exit; its paired clear_branches goes with it.

    A stale-looking lease is never enough on its own; only facts run_exited being true clears this gate.
    """
    dropped = {n for n, a in enumerate(actions) if a["kind"] == "relaunch" and a["initiative"] in not_exited}
    return [
        a
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in not_exited)
    ]


def _withhold_lost_runs(actions: list[Action], lost: frozenset[str]) -> list[Action]:
    """Drop a lost-run initiative's ordinary relaunch, retry and rescue: plan_lost_runs fully covers its recovery this tick.

    A dropped relaunch takes its paired clear_branches with it. needs_chair is untouched: a quarantine on an
    unrelated task of the same initiative still yields its own report as today.
    """
    dropped = {
        n
        for n, a in enumerate(actions)
        if a["initiative"] in lost and (a["kind"] == "relaunch" or a["kind"] in {"retry", "rescue"})
    }
    return [
        a
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in lost)
    ]


def _lease_held_ids(facts: Facts) -> frozenset[str]:
    """Initiatives whose launch was refused on a lease whose holder is still running. A gone holder waits for nothing.

    A live holder past the TTL since the last refusal is also past it since the first, so that case is the stuck one and stays held."""
    running = frozenset(r["id"] for r in facts.get("running", []))
    return frozenset(i for i, s in facts.get("lease_refusals", {}).items() if s["holder_run"] in running)


def _lease_stuck_actions(facts: Facts, now: datetime | None) -> list[Action]:
    """One lease_stuck needs_chair per initiative whose live holder has refused it for the TTL, unless its key is reported.

    The key `lease_stuck|<initiative>|<holder_run>` shares the relaunch_loop_reported set."""
    reported = set(facts.get("relaunch_loop_reported", []))
    running = frozenset(r["id"] for r in facts.get("running", []))
    due = [
        (initiative, streak)
        for initiative, streak in facts.get("lease_refusals", {}).items()
        if (first := _parse_utc(streak["first_at"])) is not None
        and now is not None
        and chair_lease_refusal.stuck_due(first, now, streak["holder_run"] in running)
        and f"lease_stuck|{initiative}|{streak['holder_run']}" not in reported
    ]
    return [
        {
            "kind": "needs_chair",
            "initiative": initiative,
            "cause": "lease_stuck",
            "reason": f"run {s['holder_run']} still holds the lease; launches refused from {s['first_at']} to {s['last_at']}",
        }
        for initiative, s in due
    ]


def _withhold_lease_held(actions: list[Action], held: frozenset[str]) -> list[Action]:
    """Drop a lease-held initiative's relaunch, retry and rescue, and the paired clear_branches. needs_chair is untouched."""
    return [
        a
        for a in actions
        if not (a["initiative"] in held and a["kind"] in ("relaunch", "retry", "rescue", "clear_branches"))
    ]


def _unmet_needs_ids(initiatives: list[InitiativeFacts]) -> frozenset[str]:
    """Initiatives no launch_epic may take this tick: no ready task has every need landed."""
    return frozenset(i["id"] for i in initiatives if _initiative_first_unmet_need(i) is not None)


def _steer_deferred(action: Action) -> bool:
    """A needs_chair raised once a steer pair's streak reached the ceiling."""
    return action["kind"] == "needs_chair" and action.get("cause") == "steer_deferred"


def _unstarted_waiting_chair(initiatives: list[InitiativeFacts]) -> list[Action]:
    """waiting-on reports for unstarted initiatives only; plan_recover's own waiting-on reports cover the
    started ones, so this never double-reports."""
    return [
        {"kind": "needs_chair", "initiative": i["id"], "cause": f"waiting on {need}"}
        for i in initiatives
        if not i["started"] and (need := _initiative_first_unmet_need(i)) is not None
    ]


def _login_needs_chair_actions(facts: Facts, now: datetime | None) -> list[Action]:
    """Login-lapsed and host-unreachable notices; host_back needs the tick's clock and is absent without it."""
    login_hosts = facts.get("login_hosts", [])
    back = chair_login_watch.host_back_needs_chair(login_hosts, now.isoformat()) if now is not None else []
    return [*chair_login_watch.login_needs_chair(login_hosts), *chair_login_watch.host_unreachable_needs_chair(login_hosts), *back]


def _held_reason(held: dict) -> str:
    """The action's reason: the diff summary when the row has one, else the export's own reason text."""
    what = f"changed with no matching store change ({held['diff_summary']})" if held.get("diff_summary") else held["reason"]
    return f"{held['path']}: {what}; keep it with `cox route import`, or restore the export"


def plan_held_files(facts: Facts) -> list[Action]:
    """One needs_chair per file the last board export held, naming its path, unless the same held text (`path|blob`)
    was already reported: a hand edit reaches the chair once, never silently, and a new edit to the file again."""
    reported = set(facts.get("held_reported", []))
    return [
        {
            "kind": "needs_chair",
            "initiative": held["initiative"],
            "cause": HELD_FILE_CAUSE,
            "path": held["path"],
            "blob": held["blob"],
            "reason": _held_reason(held),
        }
        for held in facts.get("held_files", [])
        if f"{held['path']}|{held['blob']}" not in reported
    ]


def plan_close_initiatives(facts: Facts) -> list[Action]:
    """One close_initiative per todo initiative with task rows that are all done or dropped; state is done when any task is done."""
    return [
        {"kind": "close_initiative", "initiative": entry["id"], "state": "done" if "done" in entry["task_states"] else "dropped"}
        for entry in facts.get("todo_initiatives", [])
        if entry["task_states"] and set(entry["task_states"]) <= {"done", "dropped"}
    ]


def _empty_decompose_needs_chair_actions(facts: Facts) -> list[Action]:
    """One needs_chair per empty_decompose entry not yet reported for its `<initiative>|<run>`. It only reports; the remedy is the requeue argv."""
    # `<initiative>|<run>` of each empty_decompose needs_chair already in the action log; absent means none. Not yet declared
    # on Facts, like login_hosts: the facts task fills it from chair_actions and then declares it.
    reported = set(facts.get("empty_decompose_reported", []))
    return [
        {
            "kind": "needs_chair",
            "initiative": entry["initiative"],
            "run": entry["run"],
            "cause": "empty_decompose",
            "reason": entry.get("refusal") or f"refusal line not found in the decompose log for {entry['run']}",
            "remedy": "requeue",
            "command": ["cox", "route", "requeue", entry["initiative"]],
        }
        for entry in facts.get("empty_decompose", [])
        if f"{entry['initiative']}|{entry['run']}" not in reported
    ]


def _decompose_gate(facts: Facts) -> tuple[frozenset[str], list[Action]]:
    """Intake paths whose decompose is at the empty-run limit, and one needs_chair for each not yet reported.

    The initiative is the newest run id without its `-<n>` suffix, as `chair_facts.run_initiative` takes it;
    chair_facts imports this module, so the pattern is repeated here rather than imported."""
    streaks = facts.get("decompose_streaks", {})
    reported = set(facts.get("decompose_stalled_reported", []))
    keyed = [
        (path, s, re.sub(r"-\d+$", "", s.run_ids[-1]), s.run_ids[-1]) for path, s in streaks.items() if s.run_ids
    ]
    actions: list[Action] = [
        {
            "kind": "needs_chair",
            "initiative": initiative,
            "run": run,
            "cause": "decompose_stalled",
            "reason": (
                f"decompose of {path} wrote no task items in {len(s.run_ids)} runs: {', '.join(s.run_ids)}. "
                f"{s.first_refusal or 'no refusal line was printed'}"
            ),
        }
        for path, s, initiative, run in keyed
        if f"{initiative}|{run}" not in reported
    ]
    return frozenset(streaks), actions


def _login_check_actions(facts: Facts, now: datetime | None) -> list[Action]:
    """One check_login per due host; requires the tick's clock, like housekeeping."""
    if now is None:
        return []
    login_hosts = facts.get("login_hosts", [])
    return [{"kind": "check_login", "host": name} for name in chair_login_watch.due_for_login_check(login_hosts, now.isoformat())]


def _probe_host_actions(facts: Facts, now: datetime | None) -> list[Action]:
    """One probe_host per unreachable host due for a probe; requires the tick's clock."""
    if now is None:
        return []
    login_hosts = facts.get("login_hosts", [])
    return [{"kind": "probe_host", "host": name} for name in chair_login_watch.due_for_probe(login_hosts, now.isoformat())]


def _stall_reason(last_call: LastCall | None, idle: float) -> str:
    minutes = round(idle)
    called = f"{last_call['role']} {last_call['task']}" if last_call is not None else "no node call yet"
    return f"{called}, idle {minutes}m"


def _stall_needs_chair(candidate: StallCandidate, idle: float) -> Action:
    return {
        "kind": "needs_chair",
        "initiative": candidate["initiative"],
        "run": candidate["run"],
        "cause": "stalled",
        "reason": _stall_reason(candidate["last_call"], idle),
    }


def plan_stall(candidates: list[StallCandidate], now: datetime) -> list[Action]:
    """stalled_usr1 for a local run's first stall, stalled_stop plus needs_chair for its second, needs_chair alone for a remote run."""
    actions: list[Action] = []
    for candidate in candidates:
        last_call = candidate["last_call"]
        idle = chair_stall.idle_minutes(last_call["ts"] if last_call is not None else None, candidate["started_at"], now)
        started = chair_stall.started_minutes(candidate["started_at"], now)
        if not chair_stall.is_stalled(idle, started):
            continue
        if not candidate["local"]:
            actions.append(_stall_needs_chair(candidate, idle))
        elif not candidate["usr1_sent"]:
            actions.append({"kind": "stalled_usr1", "run": candidate["run"], "initiative": candidate["initiative"]})
        else:
            actions.append({"kind": "stalled_stop", "run": candidate["run"], "initiative": candidate["initiative"]})
            actions.append(_stall_needs_chair(candidate, idle))
    return actions


def plan_idle_stall(inputs: IdleStallInputs | None, now: datetime) -> list[Action]:
    """One needs_chair for a stall whose diagnosis signature is not already the open item's."""
    if inputs is None or not is_stalled(inputs, now):
        return []
    diagnosis = diagnose(inputs)
    if inputs["open_signature"] == diagnosis["signature"]:
        return []
    return [
        {  # type: ignore[typeddict-unknown-key]  # Action has no signature field yet
            "kind": "needs_chair",
            "initiative": diagnosis["subject"],
            "cause": "idle_stall",
            "reason": diagnosis["text"],
            "signature": diagnosis["signature"],
        }
    ]


def deferred_land_deadlock(facts: Facts, now: datetime | None) -> tuple[str, ApprovedTask] | None:
    """Always None: no phase's land is deferred any more (a ticket phase lands once merge-ready), so no need waits on one.

    Callers stay until a later task retires the deferred action kind and this line with it."""
    return None


def _land_command(task: ApprovedTask) -> list[str] | None:
    from agent_tools.chair_exec import argv_for  # deferred: chair_exec imports chair_facts, which imports this module

    return argv_for({"kind": "land", "run": task["run"], "task_id": task["id"], "repo": task["repo"]})  # type: ignore[typeddict-item]


def plan_deferred_land_deadlock(facts: Facts, now: datetime | None) -> list[Action]:
    """One needs_chair naming a task that waits on an approved task whose land `land_at` defers; none while its signature is open."""
    found = deferred_land_deadlock(facts, now)
    if found is None:
        return []
    waiting, need = found
    signature = f"deferred_land:{waiting}"
    if facts["idle_stall"]["open_signature"] == signature:  # type: ignore[typeddict-item]
        return []
    command = _land_command(need)
    return [
        {  # type: ignore[typeddict-unknown-key]  # Action has no signature field yet
            "kind": "needs_chair",
            "initiative": need["initiative"],
            "cause": DEADLOCK_CAUSE,
            "reason": f"{waiting} needs approved {need['id']}; land_at defers that land",
            "signature": signature,
            **({"command": command} if command else {}),
        }
    ]


def _without_deadlocked(actions: list[Action], deadlock: tuple[str, ApprovedTask] | None) -> list[Action]:
    """`actions` less what the deadlock line replaces: the waiting action and the `waiting on <need>` needs_chair of its initiative."""
    if deadlock is None:
        return actions
    initiative = deadlock[1]["initiative"]
    return [
        a for a in actions
        if not (
            a.get("initiative") == initiative
            and (a["kind"] == "waiting" or (a["kind"] == "needs_chair" and str(a.get("cause", "")).startswith("waiting on ")))
        )
    ]


def reported_waiting(facts: Facts, now: datetime | None) -> list[WaitingInitiative]:
    """`waiting_initiatives` less the initiative a deadlock line names, for the status line's `waiting:` parts."""
    deadlock = deferred_land_deadlock(facts, now)
    return [w for w in waiting_initiatives(facts) if deadlock is None or w.initiative != deadlock[1]["initiative"]]


def _silent_dead_pid(entry: object, now: datetime) -> bool:
    """alive is False and an aware last_beat_at at least _DEAD_PID_SILENCE before now."""
    if not isinstance(entry, Mapping) or entry.get("alive") is not False:
        return False
    beat_at = entry.get("last_beat_at")
    beat = _parse_utc(beat_at) if isinstance(beat_at, str) else None
    return beat is not None and now - beat >= _DEAD_PID_SILENCE


def _dead_pid_lost(facts: Facts, now: datetime) -> dict[str, str]:
    """Initiative to its live remote run id, for a known initiative whose probe is silently dead.

    An unfetched initiative is skipped: a cleanly finished remote run also has a dead pid, and only the fetch_exit
    planned for it this tick can show whether it exited. The next tick judges it with run_exited fetched.
    """
    raw_probes = facts.get("pid_probe", {})
    probes = raw_probes if isinstance(raw_probes, Mapping) else {}
    known = {i["id"] for i in facts["initiatives"]}
    unfetched = facts.get("remote_unfetched", {})
    exited = facts.get("run_exited", {})
    remote_runs: dict[str, set[str]] = {}
    for c in facts.get("stall_candidates", []):
        if not c["local"] and c["run"]:
            remote_runs.setdefault(c["initiative"], set()).add(c["run"])
    return {
        initiative: newest_run(remote_runs[initiative])
        for initiative, entry in probes.items()
        if initiative in known
        and initiative in remote_runs
        and initiative not in unfetched
        and exited.get(initiative) is not True
        and _silent_dead_pid(entry, now)
    }


def _with_dead_pid_lost(facts: Facts, now: datetime | None) -> Facts:
    """facts with silent dead-pid initiatives merged into lost_runs; a lost_runs entry keeps its own run id."""
    if now is None:
        return facts
    return {**facts, "lost_runs": {**_dead_pid_lost(facts, now), **facts.get("lost_runs", {})}}  # type: ignore[return-value]


@dataclass(frozen=True)
class TickPlan:
    actions: list[Action]
    held_for_ci: tuple[str, ...]  # started initiatives waiting only on a land, left unrelaunched while the CI gate is paused


def _land_only_ids(initiatives: list[InitiativeFacts], approved: list[ApprovedTask]) -> frozenset[str]:
    """Started initiatives with no launchable ready task whose every unmet need is an approved task not yet landed."""
    pending = {(a["initiative"], a["id"]) for a in approved}

    def unmet(i: InitiativeFacts) -> set[str]:
        return {n for t in [*i["ready_tasks"], *i.get("waiting_tasks", [])] for n in t["needs"] if n not in i["landed"]}

    return frozenset(
        i["id"]
        for i in initiatives
        if i["started"] and _initiative_first_unmet_need(i) is not None and all((i["id"], n) in pending for n in unmet(i))
    )


def _hold_for_ci(actions: list[Action], held: frozenset[str]) -> list[Action]:
    """Drop each held initiative's relaunch and its paired clear_branches; every other action stays."""
    return [a for a in actions if not (a["kind"] in {"relaunch", "clear_branches"} and a.get("initiative") in held)]


def _gated_initiatives(facts: Facts) -> frozenset[str]:
    """Initiatives the chair neither launches, relaunches nor lands: held ones, and ones deferred to a later release."""
    return frozenset(facts.get("held_initiatives", [])) | frozenset(facts.get("deferred_initiatives", {}))


def _without_gated_launches(actions: list[Action], gated: frozenset[str]) -> list[Action]:
    """Drop each gated initiative's launch-kind actions and its clear_branches; needs_chair and mark_lost stay."""
    return [a for a in actions if not ((a["kind"] in _LAUNCHES or a["kind"] == "clear_branches") and a.get("initiative") in gated)]


def _without_gated_lands(lands: list[Action], gated: frozenset[str]) -> list[Action]:
    """Drop a gated initiative's land_phase and partial_land; a fetch, a needs_chair report and an action with no initiative stay."""
    return [a for a in lands if not (a["kind"] in ("land_phase", "partial_land") and a.get("initiative") in gated)]


def _without_widened(actions: list[Action], widened: frozenset[tuple[str, str]]) -> list[Action]:
    """Drop the needs_chair, retry and rescue of a task that gets a widen_ticket; an action with no task_id stays."""
    return [
        a
        for a in actions
        if not (a["kind"] in {"needs_chair", "retry", "rescue"} and (a.get("initiative"), a.get("task_id")) in widened)
    ]


def _task_views(initiative: InitiativeFacts, rows: Sequence[Mapping]) -> tuple[tuple[TaskView, ...], tuple[str, ...]]:
    """The initiative's tasks for `runnable_verdict`, and the sorted phases they span.

    A ready row task with no ready_tasks or waiting_tasks entry is claimed or held, so it is passed as `claimed`.
    A `merged` row state passes through to the view unchanged."""
    entries = {t["id"]: t for t in [*initiative.get("waiting_tasks", []), *initiative["ready_tasks"]]}
    in_rows = {task_id for row in rows for task_id, _ in row["tasks"]}
    from_rows = tuple(
        TaskView(
            task_id,
            row["phase"],
            "claimed" if state == "ready" and task_id not in entries else state,
            tuple(entries[task_id]["needs"]) if state == "ready" and task_id in entries else (),
        )
        for row in rows
        for task_id, state in row["tasks"]
    )
    queued = tuple(
        TaskView(t["id"], t.get("phase", ""), "ready", tuple(t["needs"])) for t in entries.values() if t["id"] not in in_rows
    )
    done = tuple(TaskView(task_id, "", "done") for task_id in sorted(initiative["landed"]) if task_id not in in_rows)
    phases = tuple(sorted({*(row["phase"] for row in rows), *(t.phase for t in queued)}))
    return (*from_rows, *queued, *done), phases


def waiting_initiatives(facts: Facts) -> list[WaitingInitiative]:
    """Initiatives with a phase_tasks row and no runnable task, by initiative id; one with no row is never blocked."""
    rows = facts.get("phase_tasks", [])
    verdicts = [
        (initiative["id"], runnable_verdict(*_task_views(initiative, mine), land_at=initiative.get("land_at", DEFAULT_LAND_AT)))
        for initiative in facts["initiatives"]
        for mine in [[row for row in rows if row["initiative"] == initiative["id"]]]
        if mine
    ]
    return [WaitingInitiative(initiative, verdict) for initiative, verdict in sorted(verdicts, key=lambda v: v[0]) if isinstance(verdict, Blocking)]


def _waiting_actions(waiting: Sequence[WaitingInitiative], reported: Mapping[str, str]) -> list[Action]:
    """One waiting action per initiative whose `<phase>|<quarantined>|<awaiting_land>` differs from what was last reported."""
    return [
        {
            "kind": "waiting",
            "initiative": w.initiative,
            "phase": w.blocking.phase,
            "quarantined": w.blocking.quarantined,
            "awaiting_land": w.blocking.awaiting_land,
        }
        for w in waiting
        if f"{w.blocking.phase}|{w.blocking.quarantined}|{w.blocking.awaiting_land}" != reported.get(w.initiative)
    ]


def _steward_handled(facts: Facts) -> frozenset[str]:
    """The epic ids the steward plans for this tick; empty when the steward key is absent or disabled."""
    steward = facts.get("steward")
    return frozenset(e["epic_id"] for e in steward["epics"]) if steward is not None and steward["enabled"] else frozenset()


_LANDS_BY_MERGE = ("level", "task")


def _land_inputs(approved: Sequence[ApprovedTask], initiative: str, phase: str) -> dict[str, object]:
    """What a phase land needs, read off the approved tasks of the phase: repo, phase, task_runs, fetch_runs and run.

    `run` is the newest run and is left out when any task's run is unknown, so the executor can name the missing run."""
    tasks = [t for t in approved if t["initiative"] == initiative and t["phase"] == phase]
    if not tasks:
        return {}
    run = newest_run({t["run"] for t in tasks})
    return {
        "repo": tasks[0]["repo"],
        "phase": phase,
        **({"run": run} if run else {}),
        "task_runs": {t["id"]: t["run"] for t in tasks},
        "fetch_runs": sorted({t["run"] for t in tasks if t["needs_fetch"] and t["run"]}),
    }


def _from_steward(action: chair_steward.StewardAction, approved: Sequence[ApprovedTask] = ()) -> Action:
    topic, fields = action["topic"], action["fields"]
    scoped: Action = {"epic": topic, "topic": topic, "fields": fields}
    if action["kind"] == "relaunch":
        # epic, topic and fields (the signature) let the executor record `relaunched`; a planner relaunch carries none.
        return {"kind": "relaunch", "initiative": topic, **scoped}
    if action["kind"] == "merge_level":
        land_at = action.get("land_at")
        inputs = _land_inputs(approved, topic, action.get("phase", "")) if land_at in _LANDS_BY_MERGE else {}
        kept = {"land_at": land_at} if land_at else {}
        return {"kind": "merge_level", **scoped, "level": int(fields["level"]), **kept, **inputs}  # type: ignore[typeddict-item]
    if action["kind"] == "run_gate":
        return {"kind": "run_gate", **scoped}
    # The only kind left is publish, which the plan names steward_event.
    return {"kind": "steward_event", **scoped, "event": action["event"]}


def _steward_actions(facts: Facts, lost: frozenset[str], gated: frozenset[str] = frozenset()) -> list[Action]:
    """The steward's decisions as actions, with no planner gate applied yet except `gated`.

    A gated epic is left out before `decide`, so its merge, gate and events go and it takes none of the one serial gate slot.

    A lost run has no exit record by definition, so the steward is told it exited: the host's silence is the evidence
    plan_lost_runs relies on, and without this a lost run on a handled epic would never be relaunched."""
    steward = facts.get("steward")
    if steward is None or not steward["enabled"]:
        return []
    epics = [{**e, "exited": True} if e["epic_id"] in lost else e for e in steward["epics"] if e["epic_id"] not in gated]
    return [_from_steward(a, facts["approved"]) for a in chair_steward.decide(epics)]


def _without_steward_lands(lands: list[Action], steward: list[Action]) -> list[Action]:
    """Drop the planner's land for each phase a steward merge_level at land_at level or task is landing.

    The merge carries the fetch_runs, so the planner's fetch of those runs goes too; otherwise the run is fetched twice."""
    merging = {(a["epic"], a["phase"]): a for a in steward if a["kind"] == "merge_level" and a.get("land_at") in _LANDS_BY_MERGE and "phase" in a}
    fetched = {(a["epic"], run) for a in merging.values() for run in a.get("fetch_runs", [])}
    return [
        a for a in lands
        if not (a["kind"] in ("land_phase", "partial_land") and (a["initiative"], a["phase"]) in merging)
        and not (a["kind"] == "fetch" and (a["initiative"], a["run"]) in fetched)
    ]


def _without_launch_pair(actions: list[Action], ids: frozenset[str]) -> list[Action]:
    """Drop the relaunch and the paired clear_branches of each initiative in ids."""
    return [a for a in actions if not (a["kind"] in ("relaunch", "clear_branches") and a["initiative"] in ids)]


def _steward_relaunches_replace(
    recover: list[Action], steward: list[Action], handled: frozenset[str]
) -> list[Action]:
    """The steward's relaunch takes the place of the planner's relaunch pair for each handled epic.

    `recover` is plan_recover's or plan_lost_runs' output; they own the gates a relaunch needs: quarantine, schema
    death, landing repo, dag problem, waiting, launch budget, loop rule and steer. The steward's relaunch is kept only
    where `recover` held one to replace, so no gate is skipped. If the planner sees no relaunchable work where the
    steward does, no relaunch is planned. The gates after (lost, unfetched, not exited, CI hold, deadlock, cap and
    homing) then see the steward's relaunch as any other.

    An epic whose steward plans a merge_level or run_gate and no relaunch of its own keeps the planner's gated relaunch,
    tagged after_steward so it runs behind that action. Its clear_branches is dropped: it could delete level branches
    before the merge."""
    gated_in = frozenset(a["initiative"] for a in recover if a["kind"] == "relaunch")
    relaunches = [a for a in steward if a["kind"] == "relaunch" and a["initiative"] in gated_in]
    own = frozenset(a["initiative"] for a in steward if a["kind"] == "relaunch")
    behind = frozenset(a["epic"] for a in steward if a["kind"] in ("merge_level", "run_gate")) - own
    kept = [{**a, "after_steward": True} for a in recover if a["kind"] == "relaunch" and a["initiative"] in behind]
    return [*_without_launch_pair(recover, handled), *relaunches, *kept]


def _no_host(free_lanes: int, host_free: Sequence[HostSlot], consumed: Mapping[str, int], filled: Sequence[Action], reserved: bool) -> bool:
    """True when no local lane and no lane-host slot is left for an epic after recovery and fill placed theirs.

    reserved is dispatch local_lanes "decompose": the local lanes then take no epic."""
    local_epics = sum(a["kind"] == "launch_epic" and "host" not in a for a in filled)
    taken = Counter(a["host"] for a in filled if a["kind"] == "launch_epic" and "host" in a)
    host_slots = [free - taken[name] for name, *_, free, _ in _less_consumed(list(host_free), dict(consumed))]
    return (reserved or free_lanes - local_epics <= 0) and not any(slot > 0 for slot in host_slots)


def _left_out_why(
    head: str, by_id: Mapping[str, InitiativeFacts], withheld_by: Mapping[str, frozenset[str]], skips: Mapping[str, str]
) -> str | None:
    """Why the launch list leaves a head out, where the facts show it; None when they show nothing."""
    named = next((why for why, ids in withheld_by.items() if head in ids), None)
    initiative = by_id.get(head)
    if named is not None:
        return named
    if head in skips:
        return f"launch budget: {skips[head]}"
    if initiative is None:
        return "not on the board"
    if initiative.get("dag_problem"):
        return "invalid dag"
    return None if initiative["ready_tasks"] else "no ready tasks"


def _stuck_steer_heads(
    heads: Sequence[str],
    launched: Collection[str],
    running: Collection[str],
    by_id: Mapping[str, InitiativeFacts],
    withheld_by: Mapping[str, frozenset[str]],
    skips: Mapping[str, str],
    no_host: bool,
) -> list[Action]:
    """One steer_head_stuck needs_chair per chain head that neither runs nor launches this tick.

    A withhold the facts show is named first: the head would not launch with lanes free. Else no host, else the
    head was left out of the launch list for a reason the facts do not show."""
    def reason(head: str) -> str:
        why = _left_out_why(head, by_id, withheld_by, skips)
        if why is not None:
            return f"launch refused: {why}"
        return "no host" if no_host else "launch refused: left out of the launch list"

    return [
        {"kind": "needs_chair", "initiative": h, "cause": "steer_head_stuck", "reason": reason(h)}
        for h in heads
        if h not in running and h not in launched
    ]


def _unlaunched_actions(
    facts: Facts,
    launches: Sequence[Action],
    withheld_by: Mapping[str, Collection[str]],
    skips: Mapping[str, str],
    free_lanes: int,
    no_host: bool,
    pace_cap: int,
    now: datetime | None,
    stuck: Sequence[Action],
) -> list[Action]:
    """One unlaunched action per ready initiative the launch list leaves out, except those with a steer_head_stuck line.

    Empty without now, as for stale and tune. Each initiative is asked on its own, so its clearing time reads from its
    own launch_history entry. host is "": an initiative left out was not placed on a lane host."""
    if now is None:
        return []
    launched = frozenset(a["initiative"] for a in launches if _is_launch(a))
    holds = [a for a in launches if a["kind"] == "steer_clear" or _steer_deferred(a)]
    reported = frozenset(a["initiative"] for a in stuck)
    history = facts.get("launch_history", {})
    return [
        action
        for i in facts["initiatives"]
        if i["id"] not in reported
        for action in chair_launch_why.unlaunched(
            [{**i, "host": ""}],
            launched,
            withheld_by,
            skips,
            holds,
            free_lanes,
            no_host,
            facts["dispatch"].get("local_lanes") == "decompose",
            pace_cap,
            now,
            history.get(i["id"], {}).get("launches", []),
        )
    ]


def _plan_as_holder(raw_facts: Facts, now: datetime | None, held: frozenset[str] = frozenset()) -> list[Action]:
    facts = _with_dead_pid_lost(raw_facts, now)
    reverts = revert_check(facts.get("landed_main", []))
    carries = plan_carry(facts.get("stranded", []))
    gated = _gated_initiatives(facts)
    planned_lands = _without_carried_lands(_without_gated_lands(plan_lands(facts, now), gated), carries)
    fetch_exits = _fetch_exit_actions(facts)
    login_needs_chair = _login_needs_chair_actions(facts, now)
    empty_decompose_needs_chair = _empty_decompose_needs_chair_actions(facts)
    held_needs_chair = plan_held_files(facts)
    stalled_intake, decompose_stalled_needs_chair = _decompose_gate(facts)
    review = plan_review(facts)
    planned_unstarted_waiting_chair = _unstarted_waiting_chair(facts["initiatives"])
    lost = frozenset(facts.get("lost_runs", {}))
    stale = plan_stale(facts, now) if now is not None else []
    tune = plan_tune(facts, now) if now is not None else []
    stall = plan_stall(facts.get("stall_candidates", []), now) if now is not None else []
    planned_idle_stall = plan_idle_stall(facts.get("idle_stall"), now) if now is not None else []
    deadlock = deferred_land_deadlock(facts, now)
    deadlock_chair = plan_deferred_land_deadlock(facts, now)
    remote_unfetched = frozenset(facts.get("remote_unfetched", {}))
    run_exited = facts.get("run_exited", {})
    widen = plan_widen(facts)
    widened = frozenset((a["initiative"], a["task_id"]) for a in widen)
    steward_handled = _steward_handled(facts)
    steward = _steward_actions(facts, lost, gated)
    # A merge_level at land_at level or task lands its phase: the planner's own land for it would be a second land.
    lands = _without_steward_lands(planned_lands, steward)
    recover_actions = _steward_relaunches_replace(plan_recover(facts, now), steward, steward_handled)
    lost_actions = _steward_relaunches_replace(plan_lost_runs(facts, now), steward, steward_handled)
    ordinary = _withhold_lost_runs(_without_widened(recover_actions, widened), lost)
    pre_exit_gate = _withhold_remote_unfetched(ordinary, remote_unfetched)
    would_relaunch = frozenset(a["initiative"] for a in pre_exit_gate if a["kind"] == "relaunch")
    not_exited = frozenset(i for i in would_relaunch if not run_exited.get(i, False))
    recoverable = _without_gated_launches(
        _hold_for_ci([*_withhold_not_exited(pre_exit_gate, not_exited), *lost_actions], held | gated), gated
    )
    lease_held = _lease_held_ids(facts)
    lease_stuck = _lease_stuck_actions(facts, now)
    waiting = waiting_initiatives(facts)
    waiting_ids = frozenset(w.initiative for w in waiting)
    # A deferred-land deadlock is one line for the whole stall: whichever blocked task `diagnose` names, its idle_stall line
    # goes, and so do the waiting action and the `waiting on <need>` reports of the deadlocked initiative.
    idle_stall = [] if deadlock is not None else planned_idle_stall
    waiting_actions = _without_deadlocked(_waiting_actions(waiting, facts.get("waiting_reported", {})), deadlock)
    unstarted_waiting_chair = _without_deadlocked(planned_unstarted_waiting_chair, deadlock)
    closes = [*plan_close_initiatives(facts), *plan_finish(facts)]
    # A blocked initiative's relaunch and its paired clear_branches go; the waiting action records why.
    recovered = _withhold_lease_held(
        [a for a in _without_deadlocked(recoverable, deadlock) if not (a["kind"] in ("relaunch", "clear_branches") and a["initiative"] in waiting_ids)],
        lease_held,
    )
    # The steward's relaunch is already in `recovered`. Its merges lead the plan: a merge_level at land_at level or task is an
    # inline land, and a land_phase started in the land sink can hold the repo for ten minutes on CI, so the merge would find it held.
    # Its gates and events follow the planner's own actions.
    steward_merges = [a for a in steward if a["kind"] == "merge_level"]
    steward_events = [a for a in steward if a["kind"] not in ("relaunch", "merge_level")]
    if facts["limits"]["hard_stop"]:
        # Nothing launches, so every ready initiative is left out; a pace cap of 0 names the first reason, which the stop replaces.
        stopped = [
            {**a, "reason": "hard stop: nothing launches until the stop lifts"}
            for a in _unlaunched_actions(facts, [], {}, {}, 0, False, 0, now, [])
        ]
        return [
            *reverts,  # a revert starts no run, so a red main is healed under the hard stop too
            *steward_merges,  # a merge starts no run either, and it must run before a land takes the repo
            *lands,
            *fetch_exits,
            *stale,
            *tune,
            *stall,
            *idle_stall,
            *deadlock_chair,
            *carries,
            *_needs_chair_only(recovered),
            *widen,
            *login_needs_chair,
            *empty_decompose_needs_chair,
            *lease_stuck,
            *held_needs_chair,
            *decompose_stalled_needs_chair,
            *review,
            *unstarted_waiting_chair,
            *waiting_actions,
            *closes,
            # Events start no run. A gate run may spend what the stop protects, so it waits for the stop to lift.
            *[a for a in steward_events if a["kind"] != "run_gate"],
            *stopped,
        ]
    cap = _launch_cap(facts["limits"])
    host_free = host_free_slots(facts)
    capped_actions, consumed = _cap_launches(
        recovered, min(cap, _dispatch_room(facts["dispatch"])), facts["initiatives"], host_free, facts.get("home", {}), facts
    )
    capped = [_with_carry(a, facts["approved"]) for a in capped_actions]
    # A hosted relaunch or retry takes no local lane, so it must not count against the local free-lane budget.
    kept = sum(a["kind"] in _LAUNCHES and "host" not in a for a in capped)
    # Recover already owns a relaunched or quarantined initiative this tick; fill must not launch it a second time.
    # Withheld initiatives stay in the facts so their ready tasks still block a pull.
    # A steer deferral already reports its initiative; fill would find the same overlap and report it twice.
    # An initiative whose repo is landing, or whose phase lands this tick, must not launch until the land settles.
    steward_started = steward_handled & frozenset(i["id"] for i in facts["initiatives"] if i["started"])
    # Each set is keyed by the plain words a stuck steer head reports when it is the reason the head is left out.
    withheld_by = {
        "recovery already holds it": frozenset(
            {a["initiative"] for a in capped if a["kind"] == "relaunch" or a["kind"] == "steer_clear" or _steer_deferred(a)}
        ),
        "quarantined": frozenset(q["initiative"] for q in facts["quarantines"]),
        "its branches are not fetched": remote_unfetched,
        "its last run has not exited": not_exited,
        "its run is lost": lost,
        "schema version death": frozenset(facts.get("schema_deaths", {})),
        "an unmet need": _unmet_needs_ids(facts["initiatives"]),
        "its repo is landing": _landing_initiatives(facts, planned_lands),
        # A relaunch the budget or the loop rule refused must not go out again as a launch_epic.
        "relaunch refused by the budget or the loop rule": refused_launches(facts, now),
        "land backoff": backoff_held(facts, now),
        "waiting on a need": waiting_ids,
        # fill would launch a started epic the steward holds back, which is a relaunch; an unstarted one still launches.
        "the steward holds it": steward_started,
        "its launch was refused on a held lease": lease_held,
        "it is held or deferred to a later release": gated,
    }
    withheld = frozenset().union(*withheld_by.values())
    claimed = claimed_by(recover_actions, facts)
    free_lanes = _free_lanes(cap, kept, facts["dispatch"])
    filled = plan_fill(facts, free_lanes, withheld, consumed, claimed, stalled=stalled_intake, now=now)
    launches = [*capped, *filled]
    skips = budget_skips(facts, facts["initiatives"], now)
    no_host = _no_host(free_lanes, host_free, consumed, filled, facts["dispatch"].get("local_lanes") == "decompose")
    stuck_heads = _stuck_steer_heads(
        steer_heads(launches),
        frozenset(a["initiative"] for a in launches if _is_launch(a)),
        frozenset(r["id"] for r in facts.get("running", [])),
        {i["id"]: i for i in facts["initiatives"]},
        withheld_by,
        skips,
        no_host,
    )
    unlaunched = _unlaunched_actions(facts, launches, withheld_by, skips, free_lanes, no_host, cap, now, stuck_heads)
    # Read before the cap: a rebase starts no run, so a relaunch the cap dropped still gets its branch rebased.
    relaunching = frozenset(a["initiative"] for a in [*recovered, *launches] if _is_launch(a))
    rebases = plan_rebase(facts.get("phase_branches", []), relaunching)
    # A relaunch tagged after_steward runs behind its epic's merge or gate, so it trails the steward events with its rebase.
    behind = [a for a in launches if a.get("after_steward")]
    behind_ids = frozenset(a["initiative"] for a in behind)
    ahead = [a for a in launches if not a.get("after_steward")]
    return [
        *reverts,
        *steward_merges,
        *lands,
        *fetch_exits,
        *stale,
        *tune,
        *stall,
        *idle_stall,
        *deadlock_chair,
        *carries,
        *_with_rebases(ahead, [r for r in rebases if r["initiative"] not in behind_ids]),
        *widen,
        *login_needs_chair,
        *empty_decompose_needs_chair,
        *lease_stuck,
        *held_needs_chair,
        *decompose_stalled_needs_chair,
        *review,
        *unstarted_waiting_chair,
        *waiting_actions,
        *stuck_heads,
        *unlaunched,
        *closes,
        *steward_events,
        *_with_rebases(behind, [r for r in rebases if r["initiative"] in behind_ids]),
    ]


def _parse_utc(ts: str | None) -> datetime | None:
    """None for an absent, unparseable or naive timestamp."""
    try:
        parsed = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def plan_housekeeping(facts: Facts, now: datetime) -> list[Action]:
    """One housekeeping action when none is recorded or the last is at least housekeeping_hours old."""
    last = _parse_utc(facts["last_housekeeping_at"])
    if last is not None and now - last < timedelta(hours=facts["housekeeping_hours"]):
        return []
    return [{"kind": "housekeeping", "reason": f"housekeeping due: last {facts['last_housekeeping_at'] if last else 'never'}"}]


def plan_tick_held(facts: Facts, now: datetime | None = None, gate: CiGate = _UNPAUSED) -> TickPlan:
    """now is the tick's clock; without it no housekeeping, check_login, probe_host or host_back is planned.

    While `gate.paused`, a started initiative that waits only on a land not yet completed gets no relaunch and is
    named in `held_for_ci`; the caller reports it. An unpaused gate, or a tick the lease gates, holds nothing."""
    lease = facts["lease"]
    gated = _lease_gate(lease)
    housekeeping = plan_housekeeping(facts, now) if now is not None else []
    login_checks = [*_login_check_actions(facts, now), *_probe_host_actions(facts, now)]
    held = _land_only_ids(facts["initiatives"], facts["approved"]) if gate.paused and gated is None else frozenset()
    holder = _withhold_blocked_hosts(_plan_as_holder(facts, now, held), facts) if gated is None else []
    # A lent lease plans the routine actions, then standby last, so the tick still reads as not acting.
    lent = gated is not None and gated[0]["kind"] == "standby" and lease.get("lent", False)
    routine = _withhold_blocked_hosts(routine_only(_plan_as_holder(facts, now)), facts) if lent else []
    actions = [*holder, *housekeeping, *login_checks] if gated is None else [*routine, *gated]
    return TickPlan([stamp(a, lease["epoch"]) for a in actions], tuple(sorted(held)))


def plan_tick(facts: Facts, now: datetime | None = None) -> list[Action]:
    """The actions of `plan_tick_held` under an unpaused gate."""
    return plan_tick_held(facts, now).actions
