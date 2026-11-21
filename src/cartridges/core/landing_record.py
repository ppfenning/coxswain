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

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Literal, TypedDict, Unpack, cast

from core.jsontypes import conform, is_mapping

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


class PullRequest(TypedDict, total=False):
    url: str
    title: str
    head: str
    base: str


class PhaseEntry(TypedDict, total=False):
    state: str
    updated_at: str
    tip: str
    merge_sha: str
    error: str
    tasks: list[str]
    conflict_paths: list[str]
    cause: str
    told: bool
    repaired_at: str  # when the lander's reconcile repaired the phase's merge on the branch
    resolved_by: str  # new model only: who resolved the merge's conflict (the merge agent, and what it said)


class WaveEntry(TypedDict, total=False):
    green_tip: str | None
    phases: list[str]


class WaveRow(TypedDict):
    wave: int
    phases: list[str]
    state: str
    green_tip: str | None


class Hold(TypedDict, total=False):
    """Why the lander holds an initiative (its record and its branch disagree), and since when."""

    reason: str
    since: str
    phase: str  # a merge hold (the new model's merge agent could not merge a phase): the phase
    tip: str  # and the tip of its branch on origin that it could not merge; a new tip lifts the hold
    cause: str  # a merge hold: why (`refused`, `no_merge_agent`, `resolver_error`, `harness_fault`, `checks_failed`, `guard`)
    attempts: int  # a merge hold: how many merges of this phase at this tip were tried and held
    last_attempt: str  # a merge hold: when the last of them was held; the automatic retry backs off from it
    released: bool  # a merge hold an operator released (`cox lander release`): the next pass tries the merge again
    drift: str  # a merge hold that reconciliation also found a drift on: the drift's reason, kept beside the hold
    pull_request_only: bool  # D2's review hold: it holds the pull request, never a phase merge (`holds_merges`)


class LandingRecord(TypedDict, total=False):
    """One initiative's landing record. Every key is optional on read: a record may predate a field."""

    initiative: str
    repo: str
    branch: str
    parent: str
    base_sha: str
    land_at: str
    state: str
    phases: dict[str, PhaseEntry]
    waves: dict[str, WaveEntry]
    pr: PullRequest | None
    epic: str | None
    stack_base: str | None
    adopted: bool
    created_at: str
    updated_at: str
    error: str
    landed_pr: str
    held: Hold | None  # set by the lander's reconcile; absent or None means not held
    retried: Hold | None  # a merge hold the sweep lifted to try again: its count, which the next hold counts on
    lane_model: str  # `phase` (the new model) when stamped so at creation; absent, or anything else, is the old model


class StateFields(TypedDict, total=False):
    """The record keys `with_state` sets beside the state."""

    pr: PullRequest | None
    error: str
    landed_pr: str


def record_from_store(raw: object) -> LandingRecord:
    """Pure. A decoded `record_json` as a `LandingRecord`; `ShapeError` when it is not an object or a key it names is
    malformed. Keys the record does not name pass, as a record may carry more than its readers name."""
    return conform(LandingRecord, raw, "landing record")


@dataclass(frozen=True)
class Unreadable:
    """A landing-record row that does not read as a record: `initiative` is the row's own `initiative` column, `why`
    the reason. `branch` is the branch the row's JSON names when it decodes far enough to say, else "": a reader that
    must know whether this row is the layer below another initiative compares it, and treats "" as unknown. `epic` is
    the epic the JSON names, "" when it names none (`epic` is null), and None when it cannot say (the row does not
    decode, or has no `epic` key, or a malformed one): a reader treats None as "maybe in any epic"."""

    initiative: str
    why: str
    branch: str = ""
    epic: str | None = None


@dataclass(frozen=True)
class Listing:
    """The landing-record table, read: the rows that read as records, and the ones that do not, each by initiative.

    One malformed row never hides the others. A reader decides what an unreadable row means for it; none of them
    writes into it, so the row stays as the evidence of what went wrong."""

    records: tuple[LandingRecord, ...] = ()
    unreadable: tuple[Unreadable, ...] = ()

    def initiatives(self) -> set[str]:
        """Every initiative with a row, readable or not: the ones that have a landing record."""
        return {str(r.get("initiative")) for r in self.records} | {u.initiative for u in self.unreadable}


def read_row(initiative: str, raw: object) -> LandingRecord | Unreadable:
    """Pure. One row of `landing_records`, from its `initiative` column and its `record_json` cell (text, bytes, an
    already-decoded value, or None), as the record or as `Unreadable` with the reason. A row whose JSON does not parse,
    is not an object, has a malformed key, or names another initiative (or none) than its column is unreadable."""
    try:
        decoded = json.loads(raw) if isinstance(raw, str | bytes | bytearray) else raw
    except ValueError as exc:
        return Unreadable(initiative, f"record_json is not JSON: {exc}")
    fields: Mapping[str, object] = decoded if is_mapping(decoded) else {}
    branch, epic = fields.get("branch"), fields.get("epic")
    named_epic = None if "epic" not in fields else "" if epic is None else epic if isinstance(epic, str) else None
    unreadable = Unreadable(initiative, "", branch if isinstance(branch, str) else "", named_epic)
    try:
        record = record_from_store(decoded)
    except ValueError as exc:
        return dataclass_replace(unreadable, why=str(exc))
    named = record.get("initiative")
    if named != initiative:
        why = "record_json names no initiative" if named is None else f"record_json names initiative {named!r}, not its row's"
        return dataclass_replace(unreadable, why=why)
    return record


def listing(rows: Iterable[tuple[str, object]]) -> Listing:
    """Pure. `(initiative column, record_json cell)` rows, in order, split into the records and the unreadable rows."""
    read = [read_row(str(initiative), raw) for initiative, raw in rows]
    return Listing(tuple(r for r in read if not isinstance(r, Unreadable)),
                   tuple(r for r in read if isinstance(r, Unreadable)))


def natural_key(name: str) -> list[int | str]:
    """`p10` after `p9`: digit runs compare as numbers."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(name))]


def phase_move_allowed(old: str, new: str) -> bool:
    return old == new or new in PHASE_MOVES.get(old, frozenset())


def initiative_move_allowed(old: str, new: str) -> bool:
    return old == new or new in INITIATIVE_MOVES.get(old, frozenset())


def holds_merges(hold: Mapping[str, object] | None) -> bool:
    """Pure. Whether a hold stops phase merges into the initiative branch: every hold does, except D2's review hold
    (`pull_request_only`), which holds only the pull request, so the work that answers it can still merge; unless
    reconciliation has kept a drift beside it, which holds everything as a drift always has."""
    if not hold:
        return False
    return hold.get("pull_request_only") is not True or bool(hold.get("drift"))


LaneModel = Literal["task", "phase"]
NEW_MODEL: LaneModel = "phase"  # the phase lane builds each phase on a worker and pushes it; the lander merges it


def lane_model_of(value: object) -> LaneModel:
    """Pure. `phase` only for the exact value `phase`; absent, `task` or anything unknown is `task`, the old model."""
    return NEW_MODEL if value == NEW_MODEL else "task"


def record_lane_model(record: LandingRecord | None) -> LaneModel:
    """Pure. The lane model a landing record was made under. A record that names none predates it: the old model."""
    return lane_model_of((record or {}).get("lane_model"))


def new_record(initiative: str, *, repo: str, branch: str, parent: str, base_sha: str, now: str,
               land_at: str = "initiative", epic: str | None = None, stack_base: str | None = None,
               adopted: bool = False, lane_model: str | None = None) -> LandingRecord:
    """A fresh record: the branch is cut, nothing is merged. `parent` is resolved here and no longer floats.

    `lane_model` is the initiative's, read once here and never changed after. Only the new model is written: an old
    model record carries no key, exactly as every record did before the key existed."""
    record = LandingRecord(
        initiative=initiative,
        repo=repo,
        branch=branch,
        parent=parent,
        base_sha=base_sha,
        land_at=land_at,
        state="building",
        phases={},
        waves={},
        pr=None,
        epic=epic,
        stack_base=stack_base,
        adopted=adopted,
        created_at=now,
        updated_at=now,
    )
    if lane_model_of(lane_model) == NEW_MODEL:
        record["lane_model"] = NEW_MODEL
    return record


def phase_parents(tasks: Iterable[Mapping[str, object]]) -> dict[str, set[str]]:
    """Phase to the phases it needs: one task of it needs a task of the other.

    The `integrate` phase needs none. Its task resolves the parent's conflict with the initiative branch, on a branch cut
    from that branch's tip: filed when a run starts (no phase builds on the old base meanwhile), it merges first, and
    filed at land time, every other phase has merged already."""
    rows = list(tasks)
    phase_of = {str(t["id"]): str(t.get("phase") or "") for t in rows}
    parents: dict[str, set[str]] = {phase: set() for phase in phase_of.values()}
    for task in rows:
        own = phase_of[str(task["id"])]
        for need in cast("Iterable[object]", task.get("needs") or ()):
            other = phase_of.get(str(need))
            if other is not None and other != own:
                parents[own].add(other)
    return parents


def phase_cycle(parents: Mapping[str, Collection[str]]) -> list[str]:
    """Pure. One cycle among the phases, as the path `[a, b, ..., a]` where each phase needs the next; [] for none.
    Phases are tried in natural order, so the same parents always name the same cycle. A phase on a cycle can never
    merge (`phase_state` reads it `waiting`), so the chair reports this as the initiative's DAG problem."""
    done: set[str] = set()

    def walk(phase: str, path: list[str]) -> list[str]:
        if phase in path:
            return [*path[path.index(phase):], phase]
        if phase in done:
            return []
        for parent in sorted(parents.get(phase, ()), key=natural_key):
            if parent != phase and (found := walk(parent, [*path, phase])):
                return found
        done.add(phase)
        return []

    for start in sorted(parents, key=natural_key):
        if found := walk(start, []):
            return found
    return []


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


def _stored(record: LandingRecord | None, phase: str) -> PhaseEntry:
    phases = (record or {}).get("phases")
    entry = phases.get(phase) if is_mapping(phases) else None
    return cast("PhaseEntry", entry) if is_mapping(entry) else {}


def phase_tasks(tasks: Iterable[Mapping[str, object]], phase: str) -> list[Mapping[str, object]]:
    return [t for t in tasks if str(t.get("phase") or "") == phase]


def phase_state(record: LandingRecord | None, phase: str, tasks: Sequence[Mapping[str, object]],
                parents: Mapping[str, Collection[str]] | None = None) -> str:
    """The state of one phase, from its stored entry and its tasks.

    `merged` is stored and final for the work that merge carried; a phase whose every task is done or dropped has
    already landed and reads `merged` too. A merged phase that has since gained work (a task sent back to build, a
    rectify task, an approval the merge did not carry: the entry's `tasks` lists what it did) is not whole on the
    initiative branch, and reads by its tasks again until it merges once more. Otherwise a phase behind an unmerged parent is `waiting`. A stored `blocked` stands while a task is
    quarantined, and a stored `conflicted` while a task is still unsettled (the rectify task). Every task settled is
    `built`, or `queued` when the queue holds it. Anything else is `open`.
    A phase whose parents lead back to itself (a cycle in the tasks' needs) waits: no order merges it first."""
    return _phase_state(record, phase, tasks, parents if parents is not None else phase_parents(tasks), frozenset())


def _phase_state(record: LandingRecord | None, phase: str, tasks: Sequence[Mapping[str, object]],
                 known: Mapping[str, Collection[str]], path: frozenset[str]) -> str:
    """`phase_state`, with `path` the phases already being asked about above this one, so a cycle ends."""
    stored = str(_stored(record, phase).get("state") or "")
    own = phase_tasks(tasks, phase)
    states = [str(t.get("state")) for t in own]
    if own and all(s in LANDED_TASK_STATES for s in states):
        return "merged"
    if stored == "merged" and not _gained_work(_stored(record, phase), own):
        return "merged"
    above = path | {phase}
    if any(
        parent in above or _phase_state(record, parent, tasks, known, above) != "merged"
        for parent in known.get(phase, ()) if parent != phase
    ):
        return "waiting"
    if stored == "blocked" and "quarantined" in states:
        return "blocked"
    settled = bool(own) and all(s in SETTLED_TASK_STATES for s in states)
    if stored == "conflicted" and not settled:
        return "conflicted"
    if settled:
        return "queued" if stored == "queued" else "built"
    return "open"


def _gained_work(entry: PhaseEntry, own: Sequence[Mapping[str, object]]) -> bool:
    """Has a merged phase work its merge did not carry: a task not settled, or an approved one the merge did not list?
    An entry with no `tasks` list (written before merges recorded it, or by hand) is trusted for approved tasks."""
    carried = entry.get("tasks")
    for task in own:
        state = str(task.get("state"))
        if state not in SETTLED_TASK_STATES:
            return True
        if state == "approved" and isinstance(carried, list) and str(task["id"]) not in carried:
            return True
    return False


def phase_states(record: LandingRecord | None, tasks: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Every phase's state, in merge order."""
    parents = phase_parents(tasks)
    return {phase: phase_state(record, phase, tasks, parents) for phase in sorted(parents, key=natural_key)}


def wave_states(record: LandingRecord | None, tasks: Sequence[Mapping[str, object]]) -> list[WaveRow]:
    """One row per wave: its number, phases, `green` once every phase is merged, and the stored green tip."""
    parents = phase_parents(tasks)
    stored = (record or {}).get("waves")
    rows: list[WaveRow] = []
    for number, members in enumerate(waves(parents), 1):
        green = all(phase_state(record, phase, tasks, parents) == "merged" for phase in members)
        entry = stored.get(str(number)) if isinstance(stored, Mapping) else None
        rows.append(WaveRow(wave=number, phases=members, state="green" if green else "open",
                            green_tip=(entry or {}).get("green_tip") if green else None))
    return rows


def placeable_phases(record: LandingRecord | None, tasks: Sequence[Mapping[str, object]]) -> list[str]:
    """The phases a build may start in: `open` or `conflicted`, so every parent phase is merged into the initiative branch."""
    return [phase for phase, state in phase_states(record, tasks).items() if state in ("open", "conflicted")]


def mergeable_phases(record: LandingRecord | None, tasks: Sequence[Mapping[str, object]]) -> list[str]:
    """The phases ready for the land queue: built, or already queued, in merge order."""
    states = phase_states(record, tasks)
    return [phase for phase in merge_order(phase_parents(tasks)) if states.get(phase) in ("built", "queued")]


def initiative_state(record: LandingRecord | None, tasks: Sequence[Mapping[str, object]]) -> str:
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


def with_phase(record: LandingRecord, phase: str, now: str, **fields: Unpack[PhaseEntry]) -> LandingRecord:
    """`record` with `fields` merged into one phase's entry. A `state` the phase table does not allow raises."""
    entry = cast("PhaseEntry", dict(_stored(record, phase)))
    new_state = fields.get("state")
    old_state = str(entry.get("state") or "open")
    if new_state is not None and not phase_move_allowed(old_state, str(new_state)):
        raise RecordError(f"phase {phase} cannot move from {old_state} to {new_state}")
    entry.update(fields)
    entry["updated_at"] = now
    return {**record, "phases": {**(record.get("phases") or {}), phase: entry}, "updated_at": now}


def with_state(record: LandingRecord, state: str, now: str, **fields: Unpack[StateFields]) -> LandingRecord:
    """`record` moved to an initiative state, with `fields` set beside it. A move the table does not allow raises."""
    old = str(record.get("state") or "building")
    if not initiative_move_allowed(old, state):
        raise RecordError(f"initiative {record.get('initiative')} cannot move from {old} to {state}")
    return {**record, **fields, "state": state, "updated_at": now}


def recorded_shas(record: LandingRecord) -> list[str]:
    """Every sha the record names on the initiative branch: the base, each phase's tip and each merge commit."""
    phases = record.get("phases") or {}
    found = [record.get("base_sha")]
    for phase in sorted(phases, key=natural_key):
        entry = phases[phase] or {}
        if entry.get("state") == "merged":
            found += [entry.get("tip"), entry.get("merge_sha")]
    return [str(sha) for sha in dict.fromkeys(found) if sha]


def merged_tasks(record: LandingRecord | None, tasks: Sequence[Mapping[str, object]]) -> list[str]:
    """The ids of approved tasks whose phase is merged into the initiative branch: the tasks that carry the `merged` flag."""
    states = phase_states(record, tasks)
    return [str(t["id"]) for t in tasks if t.get("state") == "approved" and states.get(str(t.get("phase") or "")) == "merged"]


def blocked_split(parents: Mapping[str, Collection[str]], states: Mapping[str, str], blocked: str) -> tuple[list[str], str | None]:
    """What `on_blocked: split` moves: the blocked phase and every phase that depends on it, in merge order.

    Refused, with the reason, when a phase that is already merged depends on the blocked one: its work is on the
    initiative branch and cannot leave. The `integrate` phase never moves: its task resolves this initiative's own
    branch with its parent, so a follow-up initiative would only meet the same conflict and file it again."""
    if blocked == INTEGRATE_PHASE:
        return [], f"the {INTEGRATE_PHASE} phase resolves this initiative's branch with its parent and cannot move to another"
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


def stack_resolved_parent(record: LandingRecord, table: Listing, *, stacked: bool,
                          epic_branch: str | None) -> tuple[str, Unreadable | None]:
    """Pure. (the branch `record`'s initiative takes forward now, the unreadable row that makes it unknown or None).

    The branch is its recorded parent, or, while that parent is the branch of a lower initiative that has landed,
    that initiative's own parent, layer by layer down the stack (`layer_below` at each layer). A landed initiative's
    branch is deleted, so its name is no commit to merge. A layer that may be a row that does not read stops the walk:
    whether it landed is unknown, so the caller holds rather than takes forward a branch that may be gone. The lander
    moves the record down only at land time (`_restack`, which retargets the pull request too); a run reads the same
    answer and writes nothing."""
    parent, seen = str(record.get("parent") or ""), {str(record.get("initiative"))}
    while True:
        probe: LandingRecord = {"initiative": str(record.get("initiative")), "parent": parent, "epic": record.get("epic")}
        below = layer_below(probe, table, stacked=stacked, epic_branch=epic_branch)
        if isinstance(below, Unreadable):
            return parent, below
        if below is None or str(below.get("initiative")) in seen or below.get("state") != "landed":
            return parent, None
        seen.add(str(below.get("initiative")))
        parent = str(below.get("parent") or parent)


def maybe_in_epic(found: Unreadable, epic: str, filed: str | None) -> bool:
    """Pure. Whether the row that does not read may be a member of `epic`, so the epic's pull request must wait.

    The epic its JSON names decides. When the JSON cannot say, `filed` (the epic its initiative's own file names, None
    when there is no file or it names none) decides only when it names an epic; else the row may be in any epic."""
    if found.epic is not None:
        return found.epic == epic
    return filed is None or filed == epic


def layer_below(record: LandingRecord, table: Listing, *, stacked: bool, epic_branch: str | None) -> LandingRecord | Unreadable | None:
    """Pure. The layer `record` is stacked on: the other initiative whose branch is its parent, read or unreadable.

    A readable record whose branch is the parent is the layer below. Else an unreadable row is, when its JSON names
    that branch, or, in a stacked epic (`stacked`), when it names no branch, the parent is neither the epic branch nor
    any readable record's, and the row is of the same epic or its epic is unknown: that row may be the layer below and
    nobody can tell. A row of another epic never holds this one. None when there is no layer below (the parent is a
    base branch), so the land goes on."""
    parent, initiative = str(record.get("parent") or ""), record.get("initiative")
    others = [r for r in table.records if r.get("initiative") != initiative]
    below = next((r for r in others if r.get("branch") == parent), None)
    if below is not None or not parent:
        return below
    named = next((u for u in table.unreadable if u.branch == parent and u.initiative != initiative), None)
    if named is not None or not stacked or parent == epic_branch:
        return named
    epic = record.get("epic") or ""
    return next((u for u in table.unreadable if not u.branch and u.initiative != initiative
                 and (u.epic is None or u.epic == epic)), None)
