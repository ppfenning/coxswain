"""The per-tick status line of the chair loop: a pure formatter and a thin writer.

`format_status` reads only its arguments, the time included. `write_status` prints and, when a
notify callable is injected, sends the same line. Nothing here performs an action or gathers a fact.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_tools.chair_exec import LAUNCH_KINDS, Result
from agent_tools.chair_land_freeze import LandFreeze, status_text
from agent_tools.chair_read_quarantined import RUNAWAY_CAUSE
from agent_tools.chair_runnable import WaitingInitiative
from agent_tools.chair_types import DEADLOCK_CAUSE, EASTERN, HELD_FILE_CAUSE, Action, Facts, QueueFile, ReviewPr
from agent_tools.notify import Notification
from agent_tools.queue_export import is_ahead_of_store

__all__ = [
    "EASTERN", "REASON_LINE_CAP", "Deps", "echo_line", "failed_items", "fetched_items", "file_ahead_items", "format_status",
    "housekeeping_fragment",
    "landing_fragment", "lands_this_tick", "launched_items", "mode_of", "needs_chair_items",
    "partial_land_text", "reason_tail", "steer_head_items", "steered_items", "waiting_item", "would_items", "write_status",
]

REASON_LINE_CAP = 80  # characters of a failed result's reason the one status line carries


@dataclass(frozen=True)
class Deps:
    echo: Callable[[str], None]
    notify: Callable[[Notification], None] | None = None  # absent means print only


def lands_this_tick(results: Sequence[Result]) -> int:
    """chair_exec marks a land, land_phase or partial_land `landed` only when its lands exited 0 and reported both merge and mark_done."""
    return sum(1 for r in results if r["action"].get("kind") in ("land", "land_phase", "partial_land") and r["status"] == "landed")


_TAKEBACK = "takeback from "


def takeback_holder(results: Sequence[Result]) -> str | None:
    """The holder named in a done take_lease whose reason starts `takeback from `; None when no result is one."""
    named = (
        str(r["action"].get("reason", "")).removeprefix(_TAKEBACK).split()
        for r in results
        if r["action"].get("kind") == "take_lease" and r["status"] == "done" and str(r["action"].get("reason", "")).startswith(_TAKEBACK)
    )
    return next((words[0] for words in named if words), None)


def mode_of(actions: Sequence[Action], results: Sequence[Result]) -> str:
    """dry-run when every result is a dry_run, else standby with its holder and host, else a done takeback, else holding."""
    standby = next((a for a in actions if a.get("kind") == "standby"), None)
    if results and all(r["status"] == "dry_run" for r in results):
        return "dry-run"
    if standby is not None and standby.get("until"):
        return f"standby: held by {standby.get('holder', '?').partition('@')[0]} until {standby['until']}"
    if standby is not None:
        return f"standby holder={standby.get('holder', '?')} host={standby.get('host', '?')}"
    taken_from = takeback_holder(results)
    if taken_from is not None:
        return f"takeback: took the chair back from {taken_from}"
    return "holding"


def _scope(action: Action) -> str:
    """The initiative, as `<initiative>/<phase>` when the action names a phase: a land_phase carries no task_id."""
    initiative, phase = action.get("initiative", "?"), action.get("phase", "")
    return f"{initiative}/{phase}" if phase else initiative


def _target(action: Action) -> str:
    return action.get("task_id") or _scope(action)


def partial_land_text(action: Action) -> str:
    """`partial-land: <initiative>/<phase> N approved, M quarantined` from a partial_land action's task_ids and quarantined."""
    return f"partial-land: {_scope(action)} {len(action.get('task_ids', []))} approved, {action.get('quarantined', 0)} quarantined"


def _needs_scope(action: Action) -> str:
    """The file a held_file needs_chair names, so an intake (no initiative) is still named; else `_scope`."""
    return action["path"] if action.get("cause") == HELD_FILE_CAUSE and action.get("path") else _scope(action)


def needs_chair_items(actions: Sequence[Action]) -> list[str]:
    """Runaway-ceiling entries first, each bare (`RUNAWAY <scope>`); then `needs chair: <scope>:<cause>`, a held file's
    scope being its path; then one `needs chair: <scope>:deferred_land_deadlock <reason>` per deadlock; then a `partial-land:` per partial_land."""
    chair_actions = [a for a in actions if a.get("kind") == "needs_chair"]
    runaway = [a for a in chair_actions if a.get("cause") == RUNAWAY_CAUSE]
    rest = [a for a in chair_actions if a.get("cause") not in (RUNAWAY_CAUSE, DEADLOCK_CAUSE)]
    deadlock = [a for a in chair_actions if a.get("cause") == DEADLOCK_CAUSE]
    partial = [a for a in actions if a.get("kind") == "partial_land"]
    return (
        [f"RUNAWAY {_scope(a)}" for a in runaway]
        + [f"needs chair: {_needs_scope(a)}:{a.get('cause', '?')}" for a in rest]
        + [f"needs chair: {_scope(a)}:{DEADLOCK_CAUSE} {a.get('reason', '')}" for a in deadlock]
        + [partial_land_text(a) for a in partial]
    )


def waiting_item(action: WaitingInitiative) -> str:
    """`waiting: <initiative> (blocked by <phase>: N quarantined, M awaiting land)`."""
    b = action.blocking
    return f"waiting: {action.initiative} (blocked by {b.phase}: {b.quarantined} quarantined, {b.awaiting_land} awaiting land)"


def file_ahead_items(files: Sequence[QueueFile]) -> list[str]:
    """`needs chair: <task>:file-ahead-of-store` per ticket whose file on disk no longer hashes to its stored hash."""
    return [
        f"needs chair: {f['task_id']}:file-ahead-of-store"
        for f in files
        if f["file_text"] is not None and is_ahead_of_store(f["stored_hash"], f["file_text"])
    ]


def _launched_item(action: Action) -> str:
    target = action.get("initiative") or action.get("task_id", "?")
    host = action.get("host")
    if host and action.get("kind") in ("relaunch", "retry", "launch_epic"):  # the kinds `argv_for` sends `--on` for
        return f"epic:{target}@{host}"
    return f"{action.get('kind')}:{target}"


def launched_items(results: Sequence[Result]) -> list[str]:
    """`<kind>:<initiative>` for every launch, relaunch, retry or rescue that ran; an epic launch on a lane host is `epic:<initiative>@<host>`."""
    return [
        _launched_item(r["action"])
        for r in results
        if r["status"] == "done" and r["action"].get("kind") in LAUNCH_KINDS
    ]


def reason_tail(reason: str) -> str:
    """The last non-blank line of `reason`, cut to its last REASON_LINE_CAP characters; "" when there is none."""
    lines = [line.strip() for line in reason.splitlines() if line.strip()]
    return lines[-1][-REASON_LINE_CAP:] if lines else ""


def _failed_item(result: Result) -> str:
    label = f"{result['action'].get('kind')}:{_target(result['action'])}"
    tail = reason_tail(result.get("reason", ""))
    return f"{label}: {tail}" if tail else label


def failed_items(results: Sequence[Result]) -> list[str]:
    """`<kind>:<task or initiative>` for every refused, failed or not_landed result; a land_phase is `<initiative>/<phase>`.

    A result with a reason adds `: <last line of the reason>`, so the error text is readable.
    """
    return [_failed_item(r) for r in results if r["status"] in ("refused", "failed", "not_landed")]


def steered_items(results: Sequence[Result]) -> list[str]:
    """`<initiative>~<other>` for every steer_clear result; the paths stay in the record."""
    return [
        f"{r['action'].get('initiative', '?')}~{r['action'].get('other', '?')}"
        for r in results
        if r["action"].get("kind") == "steer_clear"
    ]


def _chain_heads(node: str, behind: Mapping[str, tuple[str, ...]], seen: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """Every head `node` waits on, in first-seen order; a node in a cycle reaches none through the cycle."""
    if node not in behind:
        return (node,)
    if node in seen:
        return ()
    return tuple(dict.fromkeys(h for o in behind[node] for h in _chain_heads(o, behind, seen | {node})))


def steer_head_items(results: Sequence[Result]) -> list[str]:
    """`<head> (<n> waiting, launched|idle)` per steer chain head; launched means a done launch result for it in `results`.

    A steer_clear naming its own initiative as `other` (a sibling phase) defers nothing, so it is skipped,
    as is one missing either name. An initiative steered behind several others waits on each of their heads.
    """
    pairs = [
        (a["initiative"], a["other"])
        for a in (r["action"] for r in results)
        if a.get("kind") == "steer_clear" and a.get("initiative") and a.get("other") and a["initiative"] != a["other"]
    ]
    behind = {i: tuple(dict.fromkeys(o for j, o in pairs if j == i)) for i, _ in pairs}
    waiting = Counter(h for i in behind for h in _chain_heads(i, behind))
    launched = {
        r["action"].get("initiative")
        for r in results
        if r["status"] == "done" and r["action"].get("kind") in LAUNCH_KINDS
    }
    return [f"{head} ({n} waiting, {'launched' if head in launched else 'idle'})" for head, n in waiting.items()]


def deferred_items(results: Sequence[Result]) -> list[str]:
    """`deferred: <initiative> <phase> (deferred to the last phase, land_at <land_at>)` per deferred result; never a land or a failure."""
    return [
        f"deferred: {a.get('initiative', '?')} {a.get('phase', '?')} (deferred to the last phase, land_at {a.get('land_at', '?')})"
        for a in (r["action"] for r in results)
        if a.get("kind") == "deferred"
    ]


def landing_fragment(landing: Sequence[Mapping[str, Any]]) -> str:
    """Empty when none; else `landing N (2 coxswain, 1 dash)`, names being repo basenames (`?` for a land with no repo)."""
    if not landing:
        return ""
    names = Counter(str(land.get("repo", "")).rstrip("/").rsplit("/", 1)[-1] or "?" for land in landing)
    ordered = sorted(names.items(), key=lambda item: (-item[1], item[0]))
    return f"landing {len(landing)} ({', '.join(f'{n} {name}' for name, n in ordered)})"


def fetched_items(results: Sequence[Result]) -> list[str]:
    """`<run> from <host>` for every fetch_exit that landed; a host of None reads `on another machine`."""
    return [
        f"{r['action'].get('run', '?')} from {'on another machine' if r['action'].get('host') is None else r['action'].get('host')}"
        for r in results
        if r["status"] == "done" and r["action"].get("kind") == "fetch_exit"
    ]


def would_items(results: Sequence[Result]) -> list[str]:
    """`<kind>:<task or initiative>` for every dry_run result except standby and take_lease; a land_phase is `<initiative>/<phase>`."""
    return [
        f"{r['action'].get('kind')}:{_target(r['action'])}"
        for r in results
        if r["status"] == "dry_run" and r["action"].get("kind") not in ("standby", "take_lease")
    ]


def _five_hour(limits: dict) -> str:
    """`5h <fraction> (<source>)`; with no fraction, `5h none (<source>)` when the source is a meter (the plan has no five-hour limit), else `5h n/a`."""
    fraction = limits.get("five_hour_fraction")
    source = limits.get("window_source")
    if fraction is not None:
        return f"5h {fraction:.0%} ({source if source is not None else 'est'})"
    if source in ("meter", "meter, store"):
        return f"5h none ({source})"
    return "5h n/a"


def _weekly_pace(limits: dict) -> str:
    """` target <target>% <verdict>` for a judged pace; empty when `weekly_pace` is a `not_judged` string, absent, or has no target."""
    target, verdict = limits.get("weekly_target_fraction"), limits.get("weekly_pace")
    return "" if target is None or verdict in (None, "not_judged") else f" target {target:.0%} {verdict}"


def _weekly(limits: dict) -> str:
    """`weekly <fraction>/<hard stop fraction> (<source>)`, then the judged pace, then `since <window_start_day>` when the edge supplies one."""
    base = (
        f"weekly {limits['weekly_fraction']:.0%}/{limits['hard_stop_fraction']:.0%} ({limits.get('weekly_source', 'est')})"
        f"{_weekly_pace(limits)}"
    )
    window_start_day = limits.get("window_start_day")
    return base if window_start_day is None else f"{base} since {window_start_day}"


def housekeeping_fragment(last_housekeeping_at: str | None, now: datetime) -> str:
    """`housekeeping <age>`, minutes under an hour, hours under 48, else days; `never` when unset, unparseable or naive."""
    try:
        then = datetime.fromisoformat(last_housekeeping_at) if last_housekeeping_at else None
    except ValueError:
        return "housekeeping never"
    if then is None or then.tzinfo is None:  # a naive stamp cannot be subtracted from the tz-aware `now`
        return "housekeeping never"
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 60:
        return f"housekeeping {minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"housekeeping {hours}h"
    return f"housekeeping {hours // 24}d"


def review_fragment(review_prs: Sequence[ReviewPr]) -> str:
    """`review: N awaiting (task url, ...)`; empty when none are awaiting."""
    items = [pr["task_id"] + " " + pr["url"] for pr in review_prs]
    return f"review: {len(items)} awaiting ({', '.join(items)})" if items else ""


def _stall(facts: Facts) -> str | None:
    """`stall: <diagnosis>` while an idle stall is open; None when closed or absent."""
    diagnosis = (facts.get("idle_stall") or {}).get("open_diagnosis")
    return f"stall: {diagnosis}" if isinstance(diagnosis, str) and diagnosis else None


def _freeze(facts: Facts, now: datetime) -> str | None:
    """The shared freeze text while a freeze is live at `now`; None when absent or expired."""
    freeze = facts.get("land_freeze")
    live = isinstance(freeze, LandFreeze) and freeze.expires_at > now
    return status_text(freeze) if live else None


def epics_fragment(epics: Sequence[Mapping[str, Any]]) -> str:
    """`epics: <line>, ...` from `core.epics.format_epic_line`; empty when there is no epic."""
    from core.epics import format_epic_line  # lazy: core sits beside agent_tools in one venv

    return f"epics: {', '.join(format_epic_line(e) for e in epics)}" if epics else ""


def lanes_text(dispatch: Mapping[str, Any]) -> str:
    """A host missing live_runs or capacity adds nothing to that side of L/C."""
    here = f"{dispatch['live_runs']}/{dispatch['max_in_flight']}"
    hosts = dispatch.get("hosts") or []
    if not hosts:
        return f"lanes {here}"
    live = dispatch["live_runs"] + sum(h.get("live_runs") or 0 for h in hosts)
    cap = dispatch["max_in_flight"] + sum(h.get("capacity") or 0 for h in hosts)
    return f"lanes {live}/{cap} (here {here})"


def format_status(
    facts: Facts, all_actions: Sequence[Action | WaitingInitiative], results: Sequence[Result], now: datetime
) -> str:
    """One line per tick. `now` must be timezone-aware; it is printed in Eastern time. A WaitingInitiative is one `waiting:` part."""
    actions = [a for a in all_actions if not isinstance(a, WaitingInitiative)]
    waiting = [waiting_item(a) for a in all_actions if isinstance(a, WaitingInitiative)]
    limits, dispatch = facts["limits"], facts["dispatch"]
    stop = " hard stop" if limits["hard_stop"] else ""
    needs = [
        *needs_chair_items([*actions, *(r["action"] for r in results if r["status"] == "escalated")]),
        *file_ahead_items(facts.get("queue_files", [])),
    ]
    launched, fetched, failed, would = (
        launched_items(results), fetched_items(results), failed_items(results), would_items(results),
    )
    landing = landing_fragment(facts.get("landing", []))
    steered = steered_items(results)
    heads = steer_head_items(results)
    drafts = facts.get("drafts", 0)
    review = review_fragment(facts.get("review_prs", []))
    stall = _stall(facts)
    freeze = _freeze(facts, now)
    epics = epics_fragment(facts.get("epics", []))
    parts = [
        f"chair {now.astimezone(EASTERN):%m-%d %H:%M %Z}",
        lanes_text(dispatch),
        f"lands {lands_this_tick(results)}",
        *([f"drafts {drafts}"] if drafts > 0 else []),
        f"limits {_five_hour(dict(limits))} {_weekly(dict(limits))}{stop}",
        *([stall] if stall else []),
        *([freeze] if freeze else []),
        mode_of(actions, results),
        *([f"would: {', '.join(would)}"] if would else []),
        *([landing] if landing else []),
        *([f"launched: {', '.join(launched)}"] if launched else []),
        *([f"fetched: {', '.join(fetched)}"] if fetched else []),
        *([f"failed: {', '.join(failed)}"] if failed else []),
        *([f"steered: {', '.join(steered)}"] if steered else []),
        *([f"steer heads: {', '.join(heads)}"] if heads else []),
        *waiting,
        *deferred_items(results),
        *([review] if review else []),
        *([epics] if epics else []),
        *(f"skipped missing repo {p}" for p in facts.get("missing_repos", [])),
        ", ".join(needs) if needs else "needs chair: none",
        *([housekeeping_fragment(facts["last_housekeeping_at"], now)] if "last_housekeeping_at" in facts else []),
    ]
    return " | ".join(parts)


def _stamp(now: datetime) -> str:
    return f"chair {now.astimezone(EASTERN):%m-%d %H:%M %Z}"


def unlaunched_lines(actions: Sequence[Action], now: datetime) -> list[str]:
    """One line per unlaunched action, in the order given. `now` must be timezone-aware."""
    return [f"{_stamp(now)} not launched {a['initiative']}: {a['reason']}" for a in actions if a["kind"] == "unlaunched"]


def echo_line(line: str) -> None:
    """Edge. Flushes, because stdout redirected to a file is block-buffered and a service log would stay empty."""
    print(line, flush=True)


def write_status(line: str, deps: Deps) -> None:
    """Edge. Print the line; send it too only when a notify callable was injected."""
    deps.echo(line)
    if deps.notify is not None:
        deps.notify(Notification("chair tick", line, "low"))
