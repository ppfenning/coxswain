"""Place a `route launch` lane by the hosts table, the way the chair tick does.

Pure. The slots come from `chair_plan_fill.host_free_slots` and the choice from `chair_plan_fill.best_host`, so
this module holds no placement rule of its own. It only decides which rows are candidates: an active row, a
fresh beat (`chair_read_lost.stale_hosts`) and a workspace_dir for every host but the local one.
"""
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from agent_tools import host_cmd
from agent_tools.chair_facts import dispatch_facts
from agent_tools.chair_login_watch import login_blocked
from agent_tools.chair_plan_fill import best_host, host_free_slots
from agent_tools.chair_read_lost import stale_hosts
from agent_tools.chair_types import Facts
from agent_tools.lane_hosts import LaneHost

Row = Mapping[str, Any]


@dataclass(frozen=True)
class Placement:
    host: str
    in_use: int
    capacity: int


@dataclass(frozen=True)
class Refusal:
    """(name, in use, capacity, why it is not a candidate or '') for every host named."""

    hosts: tuple[tuple[str, int, int, str], ...]


def _capacity(row: Row | None, default: int) -> int:
    return default if row is None or row.get("capacity") is None else int(row["capacity"])


def _why_not(row: Row, local: str, stale: Collection[str], reachable: Collection[str], blocked: Collection[str]) -> str:
    name = str(row["name"])
    if row.get("state") != "active":
        return str(row.get("state"))
    if name != local and name in stale:
        return "stale beat"
    if name != local and name not in reachable:
        return "no workspace_dir"
    return "login lapsed" if name in blocked else ""


def format_refusal(refusal: Refusal) -> list[str]:
    """One line per host: `<name> <in use>/<cap>`, with the reason it was skipped after two spaces."""
    return [f"{name} {used}/{cap}" + (f"  ({why})" if why else "") for name, used, cap, why in refusal.hosts]


def place(
    rows: Sequence[Row], live_by_host: Mapping[str, int], now: datetime, local: str = "", default_capacity: int = 1,
    profile_hosts: Sequence[LaneHost] = (),
) -> Placement | Refusal:
    """The host the chair tick would give one more lane, else a refusal naming every row's in-use/cap.

    live_by_host is keyed by row name, this machine under `local`. default_capacity is the cap of a row without one."""
    active = [r for r in rows if r.get("state") == "active"]
    stale = stale_hosts(active, now.isoformat())
    reachable = set(host_cmd.dispatchable(host_cmd.host_rows_to_lane_hosts(active, profile_hosts, local))[0])
    blocked = login_blocked(rows)
    names = [str(r["name"]) for r in active if str(r["name"]) == local or (str(r["name"]) in reachable and str(r["name"]) not in stale)]
    facts = cast(Facts, {
        "dispatch": dispatch_facts(
            {"max_in_flight": default_capacity}, names, live_by_host,
            host_cmd.row_capacities(rows), host_cmd.row_weights(rows), host_cmd.row_capabilities(rows),
        ),
        "login_hosts": list(rows),
    })
    chosen = best_host(frozenset(), host_free_slots(facts))
    if chosen is not None:
        name = chosen[0]
        row = next(r for r in rows if str(r["name"]) == name)
        return Placement(name, live_by_host.get(name, 0), _capacity(row, default_capacity))
    return Refusal(tuple(
        (str(r["name"]), live_by_host.get(str(r["name"]), 0), _capacity(r, default_capacity),
         _why_not(r, local, stale, reachable, blocked))
        for r in rows
    ))


def place_local(
    local: str, rows: Sequence[Row], live_by_host: Mapping[str, int], force: bool, default_capacity: int = 1,
) -> Placement | Refusal:
    """This machine, forced. Refused at capacity unless force; no table row for it falls back to default_capacity."""
    row = next((r for r in rows if str(r.get("name")) == local), None)
    used, cap = live_by_host.get(local, 0), _capacity(row, default_capacity)
    if used < cap or force:
        return Placement(local, used, cap)
    return Refusal(((local, used, cap, ""),))
