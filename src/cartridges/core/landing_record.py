"""The landing record: one per initiative, the local record of its branch, its parent and each phase's tip.

Git stays the source for content; the record is the source for intent. Everything here is pure: the record is a
plain mapping, the task rows are the store's (`id`, `phase`, `state`, `needs`, `merged`), and time is an argument.

The states, from the landing design:

    phase       waiting -> open -> built -> queued -> merged      (conflicted and blocked send it back)
    wave        open -> green                                     (derived from its phases)
    initiative  draft -> building -> assembled -> pr_open -> landed   (conflicted and blocked likewise)

`merged` is "phase complete": the phase is merged into the initiative branch, locally. It is the only meaning of a
task's `merged` flag, and a merged task holds no files.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

PHASE_STATES = ("waiting", "open", "built", "queued", "merged", "conflicted", "blocked")
INITIATIVE_STATES = ("draft", "building", "assembled", "pr_open", "landed", "conflicted", "blocked")
WAVE_STATES = ("open", "green")
EPIC_STATES = ("open", "integrating", "complete", "landed")

# The phase an initiative-level conflict or a red pull request is rectified in. It is cut from the initiative tip
# once every other phase has merged, so it depends on all of them.
INTEGRATE_PHASE = "integrate"

SETTLED_TASK_STATES = frozenset({"approved", "done", "dropped"})
LANDED_TASK_STATES = frozenset({"done", "dropped"})

# Who may move a phase where. `waiting` and `built` are derived, never stored, so no stored move enters them: a
# stored `open` covers a built phase too, which is why it may go straight to `merged`.
PHASE_MOVES: dict[str, frozenset[str]] = {
    "waiting": frozenset({"open"}),
    "open": frozenset({"built", "queued", "merged", "blocked", "conflicted"}),
    "built": frozenset({"queued", "merged", "conflicted", "blocked"}),
    "queued": frozenset({"merged", "conflicted", "blocked", "open"}),
    "conflicted": frozenset({"open", "built", "queued", "merged", "blocked"}),
    "blocked": frozenset({"open", "built", "queued"}),
    "merged": frozenset(),
}
INITIATIVE_MOVES: dict[str, frozenset[str]] = {
    "draft": frozenset({"building"}),
    "building": frozenset({"assembled", "conflicted", "blocked"}),
    "assembled": frozenset({"pr_open", "conflicted", "building", "landed"}),
    "pr_open": frozenset({"landed", "conflicted", "building"}),
    "conflicted": frozenset({"building", "assembled"}),
    "blocked": frozenset({"building", "assembled"}),
    "landed": frozenset(),
}


class RecordError(ValueError):
    """A landing record move the state tables do not allow."""


def natural_key(name: str) -> list[Any]:
    """`p10` after `p9`: digit runs compare as numbers."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(name))]


def phase_move_allowed(old: str, new: str) -> bool:
    return old == new or new in PHASE_MOVES.get(old, frozenset())


def initiative_move_allowed(old: str, new: str) -> bool:
    return old == new or new in INITIATIVE_MOVES.get(old, frozenset())


def new_record(initiative: str, *, repo: str, branch: str, parent: str, base_sha: str, now: str,
               land_at: str = "initiative", epic: str | None = None, stack_base: str | None = None,
               adopted: bool = False) -> dict[str, Any]:
    """A fresh record: the branch is cut, nothing is merged. `parent` is resolved here and no longer floats."""
    return {
        "initiative": initiative,
        "repo": repo,
        "branch": branch,
        "parent": parent,
        "base_sha": base_sha,
        "land_at": land_at,
        "state": "building",
        "phases": {},
        "waves": {},
        "pr": None,
        "epic": epic,
        "stack_base": stack_base,
        "adopted": adopted,
        "created_at": now,
        "updated_at": now,
    }


def phase_parents(tasks: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Phase to the phases it needs: one task of it needs a task of the other. The `integrate` phase needs every other."""
    rows = list(tasks)
    phase_of = {str(t["id"]): str(t.get("phase") or "") for t in rows}
    parents: dict[str, set[str]] = {phase: set() for phase in phase_of.values()}
    for task in rows:
        own = phase_of[str(task["id"])]
        for need in task.get("needs") or ():
            other = phase_of.get(str(need))
            if other is not None and other != own:
                parents[own].add(other)
    if INTEGRATE_PHASE in parents:
        parents[INTEGRATE_PHASE] = {p for p in parents if p != INTEGRATE_PHASE}
    return parents


def waves(parents: Mapping[str, Collection[str]]) -> list[list[str]]:
    """The phases by depth: wave 1 has no parent, wave n needs a phase of wave n-1. Phases on a cycle are left out."""
    depth: dict[str, int] = {}
    pending = set(parents)
    while pending:
        placed = {p for p in pending if all(q in depth or q not in parents for q in parents[p])}
        if not placed:
            break
        for phase in placed:
            depth[phase] = 1 + max((depth[q] for q in parents[phase] if q in depth), default=0)
        pending -= placed
    top = max(depth.values(), default=0)
    return [sorted((p for p, d in depth.items() if d == level), key=natural_key) for level in range(1, top + 1)]


def merge_order(parents: Mapping[str, Collection[str]]) -> list[str]:
    """Every phase, parents first, natural order within a wave: the one order merges and adoption use."""
    return [phase for wave in waves(parents) for phase in wave]


def dependents(parents: Mapping[str, Collection[str]], phase: str) -> set[str]:
    """Every phase that needs `phase`, directly or through another phase."""
    found: set[str] = set()
    frontier = {phase}
    while frontier:
        nxt = {p for p, needs in parents.items() if p not in found and frontier & set(needs)}
        found |= nxt
        frontier = nxt
    return found


def _stored(record: Mapping[str, Any] | None, phase: str) -> Mapping[str, Any]:
    phases = (record or {}).get("phases")
    entry = phases.get(phase) if isinstance(phases, Mapping) else None
    return entry if isinstance(entry, Mapping) else {}


def phase_tasks(tasks: Iterable[Mapping[str, Any]], phase: str) -> list[Mapping[str, Any]]:
    return [t for t in tasks if str(t.get("phase") or "") == phase]


def phase_state(record: Mapping[str, Any] | None, phase: str, tasks: Sequence[Mapping[str, Any]],
                parents: Mapping[str, Collection[str]] | None = None) -> str:
    """The state of one phase, from its stored entry and its tasks.

    `merged` is stored and final; a phase whose every task is done or dropped has already landed and reads `merged`
    too. Otherwise a phase behind an unmerged parent is `waiting`. A stored `blocked` stands while a task is
    quarantined, and a stored `conflicted` while a task is still unsettled (the rectify task). Every task settled is
    `built`, or `queued` when the queue holds it. Anything else is `open`."""
    known = parents if parents is not None else phase_parents(tasks)
    stored = str(_stored(record, phase).get("state") or "")
    own = phase_tasks(tasks, phase)
    states = [str(t.get("state")) for t in own]
    if stored == "merged" or (own and all(s in LANDED_TASK_STATES for s in states)):
        return "merged"
    if any(phase_state(record, parent, tasks, known) != "merged" for parent in known.get(phase, ()) if parent != phase):
        return "waiting"
    if stored == "blocked" and "quarantined" in states:
        return "blocked"
    settled = bool(own) and all(s in SETTLED_TASK_STATES for s in states)
    if stored == "conflicted" and not settled:
        return "conflicted"
    if settled:
        return "queued" if stored == "queued" else "built"
    return "open"


def phase_states(record: Mapping[str, Any] | None, tasks: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Every phase's state, in merge order."""
    parents = phase_parents(tasks)
    return {phase: phase_state(record, phase, tasks, parents) for phase in sorted(parents, key=natural_key)}


def wave_states(record: Mapping[str, Any] | None, tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per wave: its number, phases, `green` once every phase is merged, and the stored green tip."""
    parents = phase_parents(tasks)
    stored = (record or {}).get("waves") or {}
    rows = []
    for number, members in enumerate(waves(parents), 1):
        green = all(phase_state(record, phase, tasks, parents) == "merged" for phase in members)
        entry = stored.get(str(number)) if isinstance(stored, Mapping) else None
        rows.append({"wave": number, "phases": members, "state": "green" if green else "open",
                     "green_tip": (entry or {}).get("green_tip") if green else None})
    return rows


def placeable_phases(record: Mapping[str, Any] | None, tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """The phases a build may start in: `open` or `conflicted`, so every parent phase is merged into the initiative branch."""
    return [phase for phase, state in phase_states(record, tasks).items() if state in ("open", "conflicted")]


def mergeable_phases(record: Mapping[str, Any] | None, tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """The phases ready for the land queue: built, or already queued, in merge order."""
    states = phase_states(record, tasks)
    return [phase for phase in merge_order(phase_parents(tasks)) if states.get(phase) in ("built", "queued")]


def initiative_state(record: Mapping[str, Any] | None, tasks: Sequence[Mapping[str, Any]]) -> str:
    """The initiative's state: a stored `landed` or `pr_open` stands; else blocked, conflicted, assembled or building.

    No task at all is `draft`. Every task done or dropped is `landed`, whatever the record says."""
    rows = list(tasks)
    if not rows:
        return "draft"
    stored = str((record or {}).get("state") or "")
    if stored == "landed" or all(str(t.get("state")) in LANDED_TASK_STATES for t in rows):
        return "landed"
    states = phase_states(record, rows)
    if "blocked" in states.values():
        return "blocked"
    if "conflicted" in states.values() or (stored == "conflicted" and any(s != "merged" for s in states.values())):
        return "conflicted"
    if all(state == "merged" for state in states.values()):
        return "pr_open" if stored == "pr_open" else "assembled"
    return "building"


def with_phase(record: Mapping[str, Any], phase: str, now: str, **fields: Any) -> dict[str, Any]:
    """`record` with `fields` merged into one phase's entry. A `state` the phase table does not allow raises."""
    entry = dict(_stored(record, phase))
    new_state = fields.get("state")
    old_state = str(entry.get("state") or "open")
    if new_state is not None and not phase_move_allowed(old_state, str(new_state)):
        raise RecordError(f"phase {phase} cannot move from {old_state} to {new_state}")
    entry.update(fields)
    entry["updated_at"] = now
    return {**record, "phases": {**(record.get("phases") or {}), phase: entry}, "updated_at": now}


def with_state(record: Mapping[str, Any], state: str, now: str, **fields: Any) -> dict[str, Any]:
    """`record` moved to an initiative state, with `fields` set beside it. A move the table does not allow raises."""
    old = str(record.get("state") or "building")
    if not initiative_move_allowed(old, state):
        raise RecordError(f"initiative {record.get('initiative')} cannot move from {old} to {state}")
    return {**record, **fields, "state": state, "updated_at": now}


def recorded_shas(record: Mapping[str, Any]) -> list[str]:
    """Every sha the record names on the initiative branch: the base, each phase's tip and each merge commit."""
    phases = record.get("phases") or {}
    found = [record.get("base_sha")]
    for phase in sorted(phases, key=natural_key):
        entry = phases[phase] or {}
        if entry.get("state") == "merged":
            found += [entry.get("tip"), entry.get("merge_sha")]
    return [str(sha) for sha in dict.fromkeys(found) if sha]


def merged_tasks(record: Mapping[str, Any] | None, tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """The ids of approved tasks whose phase is merged into the initiative branch: the tasks that carry the `merged` flag."""
    states = phase_states(record, tasks)
    return [str(t["id"]) for t in tasks if t.get("state") == "approved" and states.get(str(t.get("phase") or "")) == "merged"]


def blocked_split(parents: Mapping[str, Collection[str]], states: Mapping[str, str], blocked: str) -> tuple[list[str], str | None]:
    """What `on_blocked: split` moves: the blocked phase and every phase that depends on it, in merge order.

    Refused, with the reason, when a phase that is already merged depends on the blocked one: its work is on the
    initiative branch and cannot leave."""
    moving = {blocked, *dependents(parents, blocked)}
    stuck = sorted((p for p in moving if p != blocked and states.get(p) == "merged"), key=natural_key)
    if stuck:
        return [], f"merged phase(s) {', '.join(stuck)} depend on blocked phase {blocked}"
    return [p for p in merge_order(parents) if p in moving] or sorted(moving, key=natural_key), None


def follow_up_id(initiative: str, taken: Collection[str]) -> str:
    """`<id>-2` for the first split of an initiative, then `-3`: the first free name."""
    stem = re.sub(r"-(\d+)$", "", initiative) if re.search(r"-\d+$", initiative) and initiative in taken else initiative
    number = 2
    while f"{stem}-{number}" in taken or f"{stem}-{number}" == initiative:
        number += 1
    return f"{stem}-{number}"
