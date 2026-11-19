"""The chair's facts, derived: streaks and keys from the action log, initiative and dispatch facts, `gather_facts`."""

import heapq
import json
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial, reduce
from itertools import takewhile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from agent_tools import chair_lease_refusal, chair_read_docket, pacing, repo_path, route
from agent_tools.chair_decompose_miss import DecomposeMiss, next_miss
from agent_tools.chair_decompose_streak import DecomposeRun, DecomposeStreak, at_limit, streak
from agent_tools.chair_facts_read import (
    LAUNCH_KINDS,
    FactsDeps,
    Row,
    action_doc,
    awaiting_fetch,
    extra_value,
    fleet_lanes,
    float_of,
    int_of,
    land_freeze_fact,
    lease_facts,
    limits_facts,
    merge_queue_fact,
    phase_facts,
    quarantine_facts,
    run_host_facts,
    run_ordinal,
    running_initiatives,
    schedule_inputs_from_rows,
    store_approved_tasks,
    ticket_surfaces,
    weekly_fraction,
    with_attempt_runs,
    with_landing_merged,
)
from agent_tools.chair_idle_stall import IDLE_STALL_MINUTES
from agent_tools.chair_land_backoff import LandRefusal, aligned
from agent_tools.chair_launch_budget import DEFAULT_MAX_LAUNCHES_PER_HOUR, WINDOW
from agent_tools.chair_no_progress import NoProgress, RunOutcome, no_progress_facts, resolve_no_progress_after
from agent_tools.chair_plan import initiative_homes
from agent_tools.chair_plan_land import planned_tasks
from agent_tools.chair_read_dag import dag_problems
from agent_tools.chair_read_docket import Docket, DocketInitiative, docket_from_rows, str_items
from agent_tools.chair_read_intake import intake_from_rows
from agent_tools.chair_read_land_conflicts import read_land_conflicts
from agent_tools.chair_read_land_heads import read_land_heads
from agent_tools.chair_read_quarantined import quarantined_from_rows
from agent_tools.chair_read_stranded import stranded_from_rows
from agent_tools.chair_read_tuning import current_lanes, tuning_facts
from agent_tools.chair_stall_alarm import resolve_settings, stall_facts
from agent_tools.chair_types import (
    DEFAULT_QUARANTINE_HOLD_AFTER,
    HELD_FILE_CAUSE,
    STALE_CHECKOUT_CAUSE,
    ApprovalHold,
    ApprovedTask,
    CarryTask,
    DispatchFacts,
    EmptyDecomposeFacts,
    EpicFacts,
    EpicView,
    Facts,
    HeldFile,
    HeldTask,
    HoldRecord,
    HostCheck,
    HostLanes,
    IdleStallInputs,
    InitiativeFacts,
    LandedMain,
    LandFailStreak,
    LandWait,
    LandWatch,
    LaunchHistory,
    LeaseRefusalStreak,
    LivePhase,
    PhaseBranch,
    PhaseFacts,
    PidProbeFact,
    QuarantineFacts,
    RemoteUnfetched,
    RunRecord,
    StrandedPhase,
    TaskState,
    TodoInitiative,
    ViewLevel,
    ViewTask,
    WronglyReleased,
)
from agent_tools.checkout_env import CHECKOUT_ENV_KEY
from agent_tools.host_load import resolve_load_ceiling
from agent_tools.release_gate import deferred_initiatives, held_initiatives
from agent_tools.remote_lane import fetched_record_path, remote_record_path
from agent_tools.stale_draft import EXPORT_RESTORES, HELD_REFUSAL
from core.jsontypes import is_dict, is_list, is_mapping

if TYPE_CHECKING:
    from agent_tools.chair_read_idle_backlog import IdleBacklog  # chair_read_idle_backlog imports this module


Selector = str | tuple[str, str]  # a kind, or a needs_chair cause: ("needs_chair", cause)


@dataclass(frozen=True)
class ActionIndex:
    """The tick's `chair_actions` rows by the kind their doc (`action_doc`) names, and the needs_chair rows also by cause,
    each with its place in the read. A row whose kind is not a string is in `odd`, and every selection holds it."""

    kinds: Mapping[str, Sequence[tuple[int, Row]]]
    causes: Mapping[tuple[str, str], Sequence[tuple[int, Row]]]
    odd: Sequence[tuple[int, Row]]


def action_index(rows: Sequence[Row]) -> ActionIndex:
    """Pure. One pass over the rows, so each reader below reads only the rows its rule can act on, not all of them:
    about 180,000 a tick at the chair machine's volume, nearly all of them needs_chair rows of causes no rule reads."""
    kinds: dict[str, list[tuple[int, Row]]] = {}
    causes: dict[tuple[str, str], list[tuple[int, Row]]] = {}
    odd: list[tuple[int, Row]] = []
    for at, row in enumerate(rows):
        doc = action_doc(row)
        kind, cause = doc.get("kind"), doc.get("cause")
        if not isinstance(kind, str):
            odd.append((at, row))
            continue
        kinds.setdefault(kind, []).append((at, row))
        if kind == "needs_chair" and isinstance(cause, str):
            causes.setdefault((kind, cause), []).append((at, row))
    return ActionIndex(kinds, causes, odd)


def selected(index: ActionIndex, *selectors: Selector) -> list[Row]:
    """Pure. The rows of the kinds and causes named, in the order they were read. A reader given these returns what it
    returns given every row, as long as its rule leaves every other kind and cause alone."""
    parts = [index.causes.get(s, ()) if isinstance(s, tuple) else index.kinds.get(s, ()) for s in dict.fromkeys(selectors)]
    return [row for _, row in heapq.merge(*parts, index.odd, key=lambda pair: pair[0])]


def _streak_step(streaks: Mapping[str, int], doc: Row) -> dict[str, int]:
    initiative = doc.get("initiative") or doc.get("target")
    if doc.get("kind") == "steer_clear" and initiative and doc.get("other"):
        key = f"{initiative}|{doc['other']}"
        return {**streaks, key: streaks.get(key, 0) + 1}
    if doc.get("kind") in LAUNCH_KINDS and initiative:
        return {k: n for k, n in streaks.items() if k.partition("|")[0] != initiative}
    return dict(streaks)


def steer_streaks_from_actions(actions: Sequence[Row]) -> dict[str, int]:
    """`<initiative>|<other>` to its steer_clear count since that initiative's last launch; no record, no key."""
    ordered = sorted(actions, key=lambda r: str(r.get("ts") or ""))
    return reduce(_streak_step, map(action_doc, ordered), {})


HOLD_LAND_KINDS = frozenset({"land", "land_phase"})
HOLD_KINDS = HOLD_LAND_KINDS | {"steer_clear"}
HOLD_LANDED_STATUSES = frozenset({"landed", "done"})


def _hold_names(doc: Row, task: Row) -> bool:
    return doc.get("task_id") == task["id"] or (not doc.get("task_id") and doc.get("initiative") == task["initiative"])


def _hold_step(task: Row, ticks: frozenset[str], doc: Row) -> frozenset[str]:
    if doc.get("kind") not in HOLD_KINDS or not _hold_names(doc, task):
        return ticks
    if doc.get("kind") in HOLD_LAND_KINDS and doc.get("status") in HOLD_LANDED_STATUSES:
        return frozenset()
    return ticks | {str(doc.get("ts") or "")}


def approval_holds_from_actions(approved: Sequence[Row], actions: Sequence[Row]) -> list[ApprovalHold]:
    """Per approved task not merged and not branch_merged, the distinct `ts` of its or its initiative's land, land_phase and steer_clear rows since its last landed row; none, no entry."""
    docs = list(map(action_doc, sorted(actions, key=lambda r: str(r.get("ts") or ""))))
    holds = [
        ApprovalHold(
            initiative=str(a["initiative"]), repo=str(a.get("repo") or ""), task_id=str(a["id"]),
            run=str(a.get("run") or ""), ticks=len(reduce(partial(_hold_step, a), docs, frozenset())),
        )
        for a in approved
        if not a.get("merged") and not a.get("branch_merged")
    ]
    return sorted((h for h in holds if h["ticks"] > 0), key=lambda h: (h["initiative"], h["task_id"]))


def _waiting_step(reported: Mapping[str, str], doc: Row) -> dict[str, str]:
    initiative = doc.get("initiative")
    if doc.get("kind") == "waiting" and initiative:
        return {**reported, str(initiative): f"{doc.get('phase')}|{doc.get('quarantined')}|{doc.get('awaiting_land')}"}
    if doc.get("kind") in LAUNCH_KINDS and initiative:
        return {k: v for k, v in reported.items() if k != initiative}
    return dict(reported)


def waiting_reported_from_actions(actions: Sequence[Row]) -> dict[str, str]:
    """Initiative to `<phase>|<quarantined>|<awaiting_land>` of its newest waiting row, until a later launch of it."""
    ordered = sorted(actions, key=lambda r: str(r.get("ts") or ""))
    return reduce(_waiting_step, map(action_doc, ordered), {})


def pull_age_s(actions: Sequence[Row], now: datetime) -> float | None:
    """Seconds from the newest `pull` row's `ts` to `now`; None when no row is a `pull` with a parseable `ts`."""
    stamps = [
        at
        for doc in map(action_doc, actions)
        if doc.get("kind") == "pull"
        if (at := _parsed_ts(doc.get("ts"), now)) is not None
    ]
    return (now - max(stamps)).total_seconds() if stamps else None


def _parsed_ts(ts: object, now: datetime) -> datetime | None:
    try:
        return aligned(str(ts), now)
    except ValueError:
        return None


def windowed_actions(load: Callable[[datetime], Sequence[Row]], now: datetime) -> Callable[[], tuple[Row, ...]]:
    """The `FactsDeps.actions` source for one gather: `load(now)` runs on the first call, and every later call returns the same rows."""
    cell: list[tuple[Row, ...]] = []  # the one localised mutation: filled by the first call, never changed after

    def actions() -> tuple[Row, ...]:
        if not cell:
            cell.append(tuple(dict(row) for row in load(now)))
        return cell[0]

    return actions


def decompose_stalled_keys(actions: Sequence[Row]) -> list[str]:
    """`<initiative>|<run>` of each decompose_stalled needs_chair row, in row order."""
    docs = [action_doc(row) for row in actions]
    return [
        f"{d['initiative']}|{d['run']}"
        for d in docs
        if d.get("kind") == "needs_chair" and d.get("cause") == "decompose_stalled" and d.get("initiative") and d.get("run")
    ]


MISSING_IDEA_CAUSE = "decompose_missing_idea"  # the needs_chair cause for an intake whose idea file a launch kept not finding


def _miss_step(misses: Mapping[str, DecomposeMiss], doc: Row) -> dict[str, DecomposeMiss]:
    paths: object = doc.get("intake_ids") or []
    if doc.get("kind") != "launch_decompose" or not paths:
        return dict(misses)
    path = str_items(paths)[0]
    if doc.get("idea_missing") and not doc.get("fingerprint"):
        return dict(misses)  # recorded while the intake could not be read: the backoff stays as it was
    if doc.get("idea_missing"):
        missed = next_miss(misses.get(path), Path(path).stem, str(doc["idea_missing"]), str(doc.get("fingerprint") or ""), str(doc.get("ts") or ""))
        return {**misses, path: missed}
    if doc.get("status") == "done":  # the launch got past the idea file, so the run of misses is over
        return {k: v for k, v in misses.items() if k != path}
    return dict(misses)


def decompose_miss_facts(actions: Sequence[Row]) -> dict[str, DecomposeMiss]:
    """Intake path to its run of launches that found no idea file, oldest row first; a done launch of the path ends it."""
    ordered = sorted(map(action_doc, actions), key=lambda d: str(d.get("ts") or ""))
    return reduce(_miss_step, ordered, {})


class _MissFacts(TypedDict):
    decompose_misses: dict[str, DecomposeMiss]
    decompose_miss_fingerprints: dict[str, str]
    decompose_misses_reported: list[str]


def decompose_miss_fact_keys(
    actions: Sequence[Row], fingerprint: Callable[[str], str | None] | None, queued: Collection[str]
) -> _MissFacts:
    """The three decompose-miss facts: the misses, each missed intake's fingerprint now, and the reported keys.

    Only a path still in `queued` (the tick's intake) is kept or fingerprinted: one no longer queued costs nothing.
    With no fingerprint reader nothing can tell a fixed intake from an unchanged one, so there are no misses at all.
    A fingerprint that cannot be read this tick (None) keeps the recorded one, so the backoff holds as it was."""
    if fingerprint is None:
        return {"decompose_misses": {}, "decompose_miss_fingerprints": {}, "decompose_misses_reported": decompose_misses_reported_keys(actions)}
    misses = {path: miss for path, miss in decompose_miss_facts(actions).items() if path in queued}
    read = {path: fingerprint(path) for path in misses}
    return {
        "decompose_misses": misses,
        "decompose_miss_fingerprints": {path: fp if fp is not None else misses[path].fingerprint for path, fp in read.items()},
        "decompose_misses_reported": decompose_misses_reported_keys(actions),
    }


def decompose_misses_reported_keys(actions: Sequence[Row]) -> list[str]:
    """`<intake path>|<fingerprint>` of each decompose_missing_idea needs_chair row, in row order."""
    docs = [action_doc(row) for row in actions]
    return [
        f"{d['intake']}|{d.get('fingerprint', '')}"
        for d in docs
        if d.get("kind") == "needs_chair" and d.get("cause") == MISSING_IDEA_CAUSE and d.get("intake")
    ]


def held_file_keys(actions: Sequence[Row]) -> list[str]:
    """`<path>|<blob>` of each held_file needs_chair row, in row order."""
    docs = [action_doc(row) for row in actions]
    return [
        f"{d['path']}|{d.get('blob', '')}"
        for d in docs
        if d.get("kind") == "needs_chair" and d.get("cause") == HELD_FILE_CAUSE and d.get("path")
    ]


def _settled_stale_draft(doc: Row) -> bool:
    """A stale_to_draft that a later tick would only repeat: done, or refused as held or as one the export restores.
    Any other refusal (a missing initiative.md, an unknown ticket) or a failure may clear, so it is tried again."""
    reason = str(doc.get("reason") or "")
    return doc.get("status") == "done" or (doc.get("status") == "refused" and reason.startswith((HELD_REFUSAL, EXPORT_RESTORES)))


def stale_draft_keys(actions: Sequence[Row]) -> list[str]:
    """`chair_plan_stale.draft_key` of each settled stale_to_draft row (`_settled_stale_draft`), in row order."""
    docs = [action_doc(row) for row in actions]
    return [
        f"{d['initiative']}|{','.join(sorted(str_items(d.get('stale_tasks') or [])))}"
        for d in docs
        if d.get("kind") == "stale_to_draft" and d.get("initiative") and _settled_stale_draft(d)
    ]


def stale_checkout_keys(actions: Sequence[Row]) -> list[str]:
    """`<host>|<repo>` of each stale_checkout needs_chair row, in row order."""
    docs = [action_doc(row) for row in actions]
    return [
        f"{d['host']}|{d['repo']}"
        for d in docs
        if d.get("kind") == "needs_chair"
        and d.get("cause") == STALE_CHECKOUT_CAUSE
        and d.get("host")
        and d.get("repo")
    ]


def _checkout_env_of(versions_json: object) -> dict[str, object]:
    raw = versions_json if isinstance(versions_json, (str, bytes)) else None
    versions: object
    try:
        versions = json.loads(raw) if raw is not None else versions_json
    except ValueError:
        return {}
    entries = versions.get(CHECKOUT_ENV_KEY) if is_dict(versions) else None
    return dict(entries) if is_dict(entries) else {}


def host_env_from_rows(hosts: Sequence[Row]) -> dict[str, dict[str, object]]:
    """Host name to the checkout_env record in its versions_json; {} when the key is absent or the JSON unreadable."""
    return {str(h["name"]): _checkout_env_of(h.get("versions_json")) for h in hosts if h.get("name")}


def repo_dir_name(repo: str) -> str:
    """The last path component of an initiative's `repo:`, the key of the beat's checkout_env record; "" when empty."""
    return repo.rstrip("/").rpartition("/")[2]


_LOOP_RUNS = re.compile(r"runs (\S+) and (\S+) were both quarantined")


def relaunch_loop_keys(actions: Sequence[Row]) -> list[str]:
    """`<run1>|<run2>` of each relaunch_loop needs_chair row, in row order; the row names both runs only in its reason."""
    docs = [action_doc(row) for row in actions]
    matches = [
        _LOOP_RUNS.search(str(d.get("reason") or ""))
        for d in docs
        if d.get("kind") == "needs_chair" and d.get("cause") == "relaunch_loop"
    ]
    return [f"{m.group(1)}|{m.group(2)}" for m in matches if m is not None]


LAND_REFUSAL_HORIZON = timedelta(hours=24)
# Wrong belief: every unlanded land is recorded `refused` or `failed`; an exit-0 land with no merge is `not_landed`.
LAND_UNLANDED_STATUSES = ("refused", "failed", "not_landed")
LAND_FAIL_KINDS = ("land_phase", "merge_level")
LAND_FAIL_STATUSES = (*LAND_UNLANDED_STATUSES, "merge_failed")


def _within_horizon(ts: object, now: datetime, horizon: timedelta) -> bool:
    """True for an ISO `ts` in (now - horizon, now]; an unparseable `ts` is outside it."""
    try:
        at = aligned(str(ts), now)
    except ValueError:
        return False
    return now - horizon < at <= now


def land_refusals_from_actions(
    actions: Sequence[Row], now: datetime | None = None, horizon: timedelta = LAND_REFUSAL_HORIZON
) -> list[LandRefusal]:
    """Per (initiative, phase, run), the refused, failed or not_landed land_phase or partial_land rows after its last landed row and inside (now - horizon, now]; a key with none is left out. With no `now` the horizon is not applied."""
    docs = sorted((action_doc(row) for row in actions), key=lambda d: str(d.get("ts") or ""))
    lands = [d for d in docs if d.get("kind") in ("land_phase", "partial_land") and d.get("initiative") and d.get("phase") and d.get("run")]
    keys = dict.fromkeys((str(d["initiative"]), str(d["phase"]), str(d["run"])) for d in lands)

    def refusal(key: tuple[str, str, str]) -> LandRefusal | None:
        rows = [d for d in lands if (str(d["initiative"]), str(d["phase"]), str(d["run"])) == key]
        last_landed = max((n for n, d in enumerate(rows) if d.get("status") == "landed"), default=-1)
        failed = [
            d
            for d in rows[last_landed + 1 :]
            if d.get("status") in LAND_UNLANDED_STATUSES and (now is None or _within_horizon(d.get("ts"), now, horizon))
        ]
        if not failed:
            return None
        head = failed[-1].get("head")
        found: LandRefusal = {
            "initiative": key[0], "phase": key[1], "run": key[2],
            "attempts": len(failed), "last_refused_at": str(failed[-1]["ts"]),
        }
        if isinstance(head, str) and head:
            found["head"] = head
        return found

    return [r for r in map(refusal, keys) if r is not None]


_FailState = tuple[str, str, int, str]


def _fail_step(state: Mapping[tuple[str, str], _FailState], doc: Row) -> dict[tuple[str, str], _FailState]:
    key = (str(doc["initiative"]), str(doc["phase"]))
    if doc.get("status") in HOLD_LANDED_STATUSES:
        return {k: v for k, v in state.items() if k != key}
    if doc.get("status") not in LAND_FAIL_STATUSES:
        return dict(state)
    reason, ts = str(doc.get("reason") or ""), str(doc.get("ts") or "")
    _, prev_reason, ticks, prev_ts = state.get(key, ("", None, 0, ""))
    count = (ticks if ts == prev_ts else ticks + 1) if prev_reason == reason else 1
    return {**state, key: (str(doc["kind"]), reason, count, ts)}


def land_fail_streaks_from_actions(actions: Sequence[Row]) -> list[LandFailStreak]:
    """Per (initiative, phase), the newest failed land_phase or merge_level reason and the distinct ticks it ran unbroken; a landed row drops the entry."""
    docs = map(action_doc, sorted(actions, key=lambda r: str(r.get("ts") or "")))
    rows = [d for d in docs if d.get("kind") in LAND_FAIL_KINDS and d.get("initiative") and d.get("phase")]
    state = reduce(_fail_step, rows, {})
    return [
        {"initiative": i, "phase": p, "kind": "merge_level" if kind == "merge_level" else "land_phase", "reason": reason, "ticks": ticks}
        for (i, p), (kind, reason, ticks, _) in sorted(state.items())
    ]


def _no_run_step(task: Row, ticks: frozenset[str], doc: Row) -> frozenset[str]:
    kind = doc.get("kind")
    if kind == "requeue_task" and doc.get("task_id") == task["id"]:
        return frozenset()
    if kind in HOLD_LAND_KINDS and doc.get("status") in HOLD_LANDED_STATUSES and _hold_names(doc, task):
        return frozenset()
    if kind == "needs_chair" and doc.get("cause") == "no_run" and doc.get("initiative") == task["initiative"]:
        return ticks | {str(doc.get("ts") or "")}
    return ticks


def no_run_streaks_from_actions(approved: Sequence[Row], actions: Sequence[Row]) -> dict[str, int]:
    """Unmerged approved task id to the distinct ticks its initiative recorded a no_run needs_chair since the task's last landed or requeue_task row; none, no key."""
    docs = list(map(action_doc, sorted(actions, key=lambda r: str(r.get("ts") or ""))))
    counts = {str(a["id"]): len(reduce(partial(_no_run_step, a), docs, frozenset())) for a in approved if not a.get("merged")}
    return {task_id: n for task_id, n in counts.items() if n > 0}


def _launched_within_window(ts: object, now: datetime) -> bool:
    """True for an ISO `ts` in the launch budget's window, (now - WINDOW, now]; an unparseable `ts` is outside it."""
    try:
        at = aligned(str(ts), now)
    except ValueError:
        return False
    return now - WINDOW < at <= now


def launch_history_from_actions(
    actions: Sequence[Row], quarantines: Sequence[QuarantineFacts], now: datetime
) -> dict[str, LaunchHistory]:
    """Per initiative, its done launch_epic and relaunch rows in the last hour and its quarantined runs, newest first.

    A run's `body` and `main_head` are "": no gathered fact carries a ticket body hash or the default-branch head."""
    docs = [action_doc(row) for row in actions]
    launches = [
        (str(d.get("initiative") or d.get("target")), str(d["ts"]), str(d["kind"]))
        for d in docs
        if d.get("kind") in ("launch_epic", "relaunch")
        and d.get("status") == "done"
        and (d.get("initiative") or d.get("target"))
        and _launched_within_window(d.get("ts"), now)
    ]
    quarantined = {(q["initiative"], run): q for q in reversed(quarantines) if (run := q.get("run"))}
    initiatives = sorted({initiative for initiative, _, _ in launches} | {i for i, _ in quarantined})

    def runs_of(initiative: str) -> list[RunRecord]:
        own = sorted((r for i, r in quarantined if i == initiative), key=run_ordinal, reverse=True)
        return [
            {"run_id": r, "quarantined": True, "reason": quarantined[(initiative, r)].get("reason", ""), "body": "", "main_head": ""}
            for r in own
        ]

    return {
        i: {"launches": [{"at": at, "kind": kind} for initiative, at, kind in launches if initiative == i], "runs": runs_of(i)}
        for i in initiatives
    }


def lease_refusals_from_actions(actions: Sequence[Row], now: datetime) -> dict[str, LeaseRefusalStreak]:
    """Per initiative, its newest unbroken run of lease_held launch rows; rows outside the launch window are dropped first.

    Any later launch_epic or relaunch row with another status ends the run. A row whose reason does not parse is skipped."""
    launches = sorted(
        (
            d
            for d in map(action_doc, actions)
            if d.get("kind") in ("launch_epic", "relaunch")
            and (d.get("initiative") or d.get("target"))
            and _launched_within_window(d.get("ts"), now)
        ),
        key=lambda d: str(d["ts"]),
    )

    def streak_of(initiative: str) -> LeaseRefusalStreak | None:
        own = [d for d in launches if (d.get("initiative") or d.get("target")) == initiative]
        held = takewhile(lambda d: d.get("status") == chair_lease_refusal.STATUS, reversed(own))
        parsed = [(d, p) for d in held if (p := chair_lease_refusal.parse_lease_refusal(str(d.get("reason") or ""))) is not None]
        if not parsed:
            return None
        return {"first_at": str(parsed[-1][0]["ts"]), "last_at": str(parsed[0][0]["ts"]), "holder_run": parsed[0][1].holder_run}

    found = {i: streak_of(i) for i in sorted({str(d.get("initiative") or d.get("target")) for d in launches})}
    return {i: s for i, s in found.items() if s is not None}


def decompose_streak_facts(
    paths: Sequence[str],
    runs_for: Callable[[str], Sequence[DecomposeRun]],
    cutoff_for: Callable[[str], datetime | None] | None = None,
) -> dict[str, DecomposeStreak]:
    """Intake path to its streak, for only the paths whose trailing empty runs reach the limit.

    `cutoff_for` gives an intake's last release or requeue time; runs launched before it are not counted."""
    since: Callable[[str], datetime | None] = cutoff_for or (lambda path: None)
    streaks = {path: streak(runs_for(path), since(path)) for path in paths}
    return {path: s for path, s in streaks.items() if at_limit(s)}


def read_ticket_items(ws: Path, mode: str) -> list[dict[str, object]]:
    """Edge. Every ticket under `ws/work`, parsed as the docket reader parses it, its state from the store in "store" mode."""
    return chair_read_docket.docket_work_items(ws, mode)


def read_initiative_repos(ws: Path, home: str) -> dict[str, str]:
    """Edge. Initiative id to the `repo:` in `work/<initiative>/initiative.md` expanded under `home`; an unreadable file or no `repo:` is left out."""
    texts = {p.parent.name: chair_read_docket.docket_text(p) for p in sorted((ws / "work").glob("*/initiative.md"))}
    fields = {name: route.parse_frontmatter(text)[0] for name, text in texts.items() if text is not None}
    return {name: repo_path.expand(repo, home) for name, f in fields.items() if isinstance(repo := f.get("repo"), str) and repo}


def land_at_facts(cartridge: Mapping[str, object], values: Mapping[str, str | None], ids: Sequence[str]) -> dict[str, str]:
    """Initiative id to the land_at `core.landing.resolve_land_at` gives its cartridge and `initiative.md` value.

    A value the resolver refuses reads `ticket`, the default, so one bad file never stops a tick."""
    from core.landing import DEFAULT_LAND_AT, resolve_land_at  # lazy: core sits beside agent_tools in one venv

    def resolved(initiative: str) -> str:
        try:
            return resolve_land_at(cartridge, values.get(initiative))
        except ValueError:
            return DEFAULT_LAND_AT

    return {i: resolved(i) for i in ids}


def docket_by_land_at(
    items: Sequence[Row], busy_lanes: int, max_in_flight: int, land_at: Mapping[str, str]
) -> Docket:
    """`docket_from_builder` over `items`, each initiative's summary taken under its own land_at.

    `route.initiative_summaries` takes one land_at, so it runs once per initiative. An id missing from `land_at` reads ticket."""
    from core.landing import DEFAULT_LAND_AT  # lazy: core sits beside agent_tools in one venv

    summaries = [
        summary
        for initiative in sorted({str(i["initiative"]) for i in items})
        for summary in route.initiative_summaries(
            [i for i in items if i["initiative"] == initiative], land_at=land_at.get(initiative, DEFAULT_LAND_AT)
        )
    ]
    return chair_read_docket.docket_from_builder(summaries, list(items), busy_lanes, max_in_flight, land_at)


def initiative_facts(
    docket: Docket,
    live: Collection[str],
    surfaces: Mapping[str, list[str]] | None = None,
    repos: Mapping[str, str] | None = None,
    land_at: Mapping[str, str] | None = None,
    rows: Mapping[str, Sequence[Row]] | None = None,
    claimed: Collection[tuple[str, str]] = (),
    live_phases: Collection[tuple[str, str]] = (),
) -> list[InitiativeFacts]:
    """The docket's initiatives with no live run, so a live run is never relaunched.

    `repo`, `ready_surfaces`, `land_at` and `phases` ride along only when their source was read; `rows` is each initiative's
    ticket rows. `phases` leaves out a phase in `live_phases` and a task in `claimed` (initiative, task id), and is absent
    when no phase can start a lane, so an initiative with no phase rows reads as it did."""
    from core.landing import DEFAULT_LAND_AT  # lazy: core sits beside agent_tools in one venv

    def phases_entry(initiative: str) -> list[PhaseFacts]:
        if rows is None or initiative not in rows:
            return []
        held = {t for i, t in claimed if i == initiative}
        live_here = {p for i, p in live_phases if i == initiative}
        listed = phase_facts(rows[initiative], (land_at or {}).get(initiative, DEFAULT_LAND_AT), held)
        return [p for p in listed if p["phase"] not in live_here]

    def entry(i: DocketInitiative) -> InitiativeFacts:
        facts: InitiativeFacts = {
            "id": i["id"],
            "started": bool(i["started"]),
            "ready_tasks": [
                {"id": t["id"], "phase": t["phase"], "needs": list(t["needs"]), "requires": list(t.get("requires", []))}
                for t in i["ready_tasks"]
            ],
            "waiting_tasks": [
                {"id": t["id"], "phase": t["phase"], "needs": list(t["needs"]), "requires": list(t.get("requires", []))}
                for t in i.get("waiting_tasks", [])
            ],
            "landed": set(i["landed"]),
        }
        if surfaces is not None:
            facts["ready_surfaces"] = list(surfaces.get(i["id"], []))
        if repos is not None and i["id"] in repos:
            facts["repo"] = repos[i["id"]]
            facts["repo_dir"] = repo_dir_name(repos[i["id"]])
        if land_at is not None and i["id"] in land_at:
            facts["land_at"] = land_at[i["id"]]
        shown = phases_entry(i["id"])
        if shown:
            facts["phases"] = shown
        return facts

    return [entry(i) for i in docket["initiatives"] if i["id"] not in live]


def empty_decompose_facts(
    decomposed: Mapping[str, str],
    live: Collection[str],
    item_counts: Mapping[str, int],
    states: Mapping[str, str] | None = None,
    alive: Collection[str] = frozenset(),
    refusals: Mapping[str, str] = {},
    remote_pending: Collection[str] = frozenset(),
) -> list[EmptyDecomposeFacts]:
    """One entry per decomposed intake whose decompose run has ended and left zero stored task items.

    `decomposed` maps an initiative id to its newest decompose run id, for every initiative whose intake
    status is decomposed. An initiative in `live` still has that run in flight, so it is left out even
    though its intake is decomposed. `item_counts` defaults a missing initiative to 1, non-empty, so an
    initiative this tick knows nothing about is never reported empty by omission. `states` maps an initiative
    id to its store state: done or dropped is left out, and an initiative with no known state is reported.
    `alive` holds decompose run ids whose process still runs: the runs row can end before the process has
    written every item, so an initiative whose newest run is in `alive` is left out like one in `live`.
    `refusals` maps a run id to its lint-refusal text: only an entry whose run is listed gains a `refusal` key.
    `remote_pending` holds decompose run ids whose remote task items are unfetched or fetched this tick: left out like `alive`."""
    known = states or {}
    facts: list[EmptyDecomposeFacts] = []
    for initiative, run in decomposed.items():
        if (
            initiative not in live
            and run not in alive
            and run not in remote_pending
            and item_counts.get(initiative, 1) == 0
            and known.get(initiative) not in {"done", "dropped"}
        ):
            entry: EmptyDecomposeFacts = {"initiative": initiative, "run": run}
            if run in refusals:
                entry["refusal"] = refusals[run]
            facts.append(entry)
    return facts


def alive_decompose_runs(
    runs_dir: str, decomposed: Mapping[str, str], pid_probe: Mapping[str, PidProbeFact]
) -> set[str]:
    """Edge. The newest decompose runs whose process is alive, probed from `<runs_dir>/<run>.pid` itself.

    Not `epic.run_live`: it returns the lease verdict whenever a lease row exists, so an ended run whose process is
    still writing would read as dead. A missing or unreadable pid file is not alive; so is an empty `runs_dir`."""
    from agent_tools.chair_read_lost import pid_alive  # lazy: chair_read_lost imports this module

    return {
        run
        for initiative, run in decomposed.items()
        if ((probed := pid_probe.get(initiative)) is not None and probed.get("alive"))
        or (bool(runs_dir) and pid_alive(Path(runs_dir) / f"{run}.pid"))
    }


def remote_pending_decompose_runs(runs_dir: str, decomposed: Mapping[str, str], now: datetime) -> set[str]:
    """Edge. The newest decompose runs that ran on a lane host and whose items are not yet safely fetched.

    Pending: `<run>.remote.json` exists and `<run>.fetched.json` is absent or was written at or after `now`.
    A run with no remote marker is never pending; so is anything under an empty `runs_dir`."""
    if not runs_dir:
        return set()
    base = Path(runs_dir)

    def pending(run: str) -> bool:
        if not remote_record_path(base, run).exists():
            return False
        fetched = fetched_record_path(base, run)
        return not fetched.exists() or datetime.fromtimestamp(fetched.stat().st_mtime, tz=now.tzinfo) >= now

    return {run for run in decomposed.values() if pending(run)}


def dispatch_facts(
    docket: Mapping[str, object],
    lane_hosts: Sequence[str],
    live_by_host: Mapping[str, int],
    capacity: Mapping[str, int] | None = None,
    weight: Mapping[str, int] | None = None,
    capabilities: Mapping[str, Sequence[str]] | None = None,
    local: str = "",
    local_lanes: str = "any",
) -> DispatchFacts:
    """live_by_host maps a host name to its live lanes; the local machine is under the empty name. A host in `capacity`
    carries its own cap. `weight` and `capabilities` come from the same hosts table row; a name missing from
    `capabilities` has none, and a name missing from `weight` carries no weight, so placement weights it by capacity.
    `local` names the machine running this loop: an entry in `lane_hosts` equal to it is never added to the
    returned `hosts`, since the loop's own machine is never a remote lane-host candidate for itself.
    `local_lanes` is carried only when it is not "any", so the default facts stay as they were."""
    caps = capacity or {}
    weights = weight or {}
    abilities = capabilities or {}
    def lanes_of(name: str) -> HostLanes:
        lanes: HostLanes = {"name": name, "live_runs": live_by_host.get(name, 0)}
        if name in caps:
            lanes["capacity"] = caps[name]
        if name in weights:
            lanes["weight"] = weights[name]
        lanes["capabilities"] = list(abilities.get(name, []))
        return lanes

    facts: DispatchFacts = {
        "max_in_flight": int_of(docket["max_in_flight"]),
        "live_runs": live_by_host.get("", 0),
        "hosts": [lanes_of(name) for name in lane_hosts if name != local],
    }
    if local_lanes != "any":
        facts["local_lanes"] = local_lanes
    return facts


def approved_facts(
    rows: Sequence[ApprovedTask],
    land_at: Mapping[str, str] | None = None,
    branch_merged: Callable[[ApprovedTask], bool] | None = None,
) -> list[ApprovedTask]:
    """Each row also carries its initiative's `land_at` (an id missing from `land_at` reads ticket) and `branch_merged`.

    `branch_merged` is asked only for land_at ticket or level, and is given the row with that `land_at` set."""
    def at(r: ApprovedTask) -> str:
        return (land_at or {}).get(r["initiative"], "initiative")

    def on_branch(r: ApprovedTask) -> bool:
        return branch_merged is not None and at(r) != "task" and bool(branch_merged({**r, "land_at": at(r)}))

    return [
        {
            "id": r["id"],
            "initiative": r["initiative"],
            "repo": r["repo"],
            "phase": r["phase"],
            "phase_done": bool(r["phase_done"]),
            "needs": list(r["needs"]),
            "run": str(r["run"]),
            "needs_fetch": bool(r["needs_fetch"]),
            "merged": bool(r.get("merged", False)),
            "land_at": at(r),
            "branch_merged": on_branch(r),
        }
        for r in rows
    ]


RELEASE_NOTE = "Released from approval on"
# The cause `apply_release` carries in its note: the ticket's wording, and the reason text the retired hold timer wrote.
RELEASE_CAUSES = ("approval unmerged for", "approval not merged to main or its phase branch for")


def _release_note(body: object) -> bool:
    return any(
        line.startswith(RELEASE_NOTE) and any(cause in line for cause in RELEASE_CAUSES)
        for line in str(body).splitlines()
    )


def wrongly_released_facts(
    items: Sequence[Row], branch_merged: Callable[[Row], bool], land_at: Mapping[str, str]
) -> list[WronglyReleased]:
    """Ready tasks whose body carries the hold's release note, under ticket or level landing, with `branch_merged` true.

    An initiative missing from `land_at` reads ticket. `branch_merged` is asked only of a task that passed the rest."""
    return [
        {"initiative": str(i["initiative"]), "task_id": str(i["id"])}
        for i in items
        if i.get("state") == "ready"
        and _release_note(i.get("body", ""))
        and land_at.get(str(i["initiative"]), "initiative") != "task"
        and branch_merged(i)
    ]


def todo_initiative_facts(rows: Sequence[Row]) -> list[TodoInitiative]:
    """One entry per todo initiative row, in row order. A task row is any row of that initiative whose kind is neither initiative nor intake."""
    tasks = [r for r in rows if r.get("kind") not in ("initiative", "intake")]
    return [
        {"id": str(r["initiative"]), "task_states": [str(t["state"]) for t in tasks if t["initiative"] == r["initiative"]]}
        for r in rows
        if r.get("kind") == "initiative" and r.get("state") == "todo"
    ]


def epic_facts(rows: Sequence[Row], definitions: Mapping[str, Mapping[str, object]]) -> list[EpicFacts]:
    """`core.epics.rollup` over the store's work rows; an initiative with no epic is left out.

    Landed is `route.initiative_states`: at least one task row and every one done or dropped."""
    from agent_tools.queue_rows import epic_of_row
    from core.epics import rollup  # lazy: core sits beside agent_tools in one venv

    initiatives = [r for r in rows if r.get("kind") == "initiative"]
    ids = [str(r["initiative"]) for r in initiatives]
    tasks = [r for r in rows if r.get("kind") == "task"]
    landed = {i for i, done in route.initiative_states(ids, tasks).items() if done}
    members = {str(r["initiative"]): epic_of_row(r) for r in initiatives}
    return [
        {
            "id": r["id"],
            "title": r["title"] if isinstance(r["title"], str) else str(r["title"]),
            "target_branch": r["target_branch"] if isinstance(r["target_branch"], str) else None,
            "initiatives": r["initiatives"],
            "landed": r["landed"],
            "total": r["total"],
            "complete": r["complete"],
        }
        for r in rollup(members, landed, definitions)
    ]


def phase_branch_facts(rows: Sequence[Row]) -> list[PhaseBranch]:
    """One PhaseBranch per row, in row order; ahead and behind are counts against main."""
    return [
        {
            "initiative": str(r["initiative"]),
            "phase": str(r["phase"]),
            "branch": str(r["branch"]),
            "ahead": int_of(r["ahead"]),
            "behind": int_of(r["behind"]),
            "tip": str(r["tip"]),
        }
        for r in rows
    ]


def stranded_facts(
    approved: Sequence[ApprovedTask], commits: Sequence[Row], phases: Sequence[Row]
) -> list[StrandedPhase]:
    """A phase with approved work that adds nothing over main or whose approvals span several runs.

    `commits` is oldest first, so the last row for a task in a run is its newest commit. Carry rows sort by task
    then run_seq; `pending` is the phase's task ids with no approved row, in the phase's order."""
    out: list[StrandedPhase] = []
    for ph in sorted(phases, key=lambda p: (str(p["initiative"]), str(p["phase"]))):
        init, phase = str(ph["initiative"]), str(ph["phase"])
        mine = {a["id"]: a for a in approved if a["initiative"] == init and a["phase"] == phase}
        newest = {
            (str(c["task"]), str(c["run"])): c
            for c in commits
            if c["initiative"] == init and str(c["phase"]) == phase and c["task"] in mine
        }
        rows: list[CarryTask] = sorted(
            (
                {
                    "task": str(c["task"]),
                    "run": str(c["run"]),
                    "host": str(c["host"]),
                    "branch": str(c["branch"]),
                    "commit": str(c["commit"]),
                    "needs": list(mine[str(c["task"])]["needs"]),
                    "run_seq": int_of(c["run_seq"]),
                }
                for c in newest.values()
            ),
            key=lambda r: (r["task"], r["run_seq"]),
        )
        if rows and (not ph["adds"] or len({r["run"] for r in rows}) > 1):
            out.append(
                {
                    "initiative": init,
                    "phase": phase,
                    "phase_branch": f"epic/{init}/{phase}",
                    "approved": rows,
                    "pending": [t for t in str_items(ph["tasks"]) if t not in mine],
                }
            )
    return out


def new_missing_repos(missing: Collection[str], reported: Collection[str]) -> list[str]:
    """The paths in `missing` not in `reported`, de-duplicated and sorted."""
    return sorted({p for p in missing if p not in reported})


def intake_path_of_row(row: Row) -> str:
    """The intake file a queued row names: its recorded `extra.path` when it has one, else `intake/<task_id>.md`
    (`queue_rows.row_path` for a queued row). The row's key is the bare stem, so the `intake/` folder comes from here.
    A relative path resolves against the workspace, the cwd every chair launch runs in."""
    extra = row["extra"]
    recorded = extra.get("path") if is_mapping(extra) else None
    return recorded if isinstance(recorded, str) and recorded else f"intake/{row['task_id']}.md"


def intake_paths_from_rows(rows: Sequence[Row], current: str | None = None) -> list[str]:
    """Queued intake rows as workspace-relative paths (`intake/<task_id>.md`), in row order: the shape `read_intake` returns.

    Held intakes are dropped; with `current`, so are those whose `target_release` is above it."""
    return [intake_path_of_row(row) for row in intake_from_rows(rows, current)]


DEFAULT_HOUSEKEEPING_HOURS = 24.0
DEFAULT_STALE_DAYS = 7  # chair.stale_days profile default
DEFAULT_SHARED_FILE_LIMIT = 2  # chair.max_shared_file_initiatives profile default


def resolve_housekeeping_hours(value: object) -> float:
    """The profile's raw `chair.housekeeping_hours`, or 24 when it is missing, non-numeric, or not positive."""
    try:
        hours = float_of(value)
    except (TypeError, ValueError):
        return DEFAULT_HOUSEKEEPING_HOURS
    return hours if hours > 0 else DEFAULT_HOUSEKEEPING_HOURS


def resolve_stale_days(value: object) -> int:
    """The profile's raw `chair.stale_days`, or 7 when it is missing or non-numeric."""
    try:
        return int_of(value)
    except (TypeError, ValueError):
        return DEFAULT_STALE_DAYS


def resolve_max_launches_per_hour(value: object) -> int:
    """The profile's raw `chair.max_launches_per_hour`, or the default when it is missing, non-integer, or not positive."""
    try:
        limit = int_of(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_LAUNCHES_PER_HOUR
    return limit if limit > 0 and not isinstance(value, bool) else DEFAULT_MAX_LAUNCHES_PER_HOUR


def _no_progress(deps: FactsDeps, rows: Sequence[Row], started: Sequence[str]) -> dict[str, NoProgress]:
    """The no-progress fact for the started initiatives; {} without a run reader or with the guard off."""
    after = resolve_no_progress_after(deps.no_progress_after() if deps.no_progress_after is not None else None)
    if deps.run_outcomes is None or after == 0 or not started:
        return {}
    return no_progress_from(rows, deps.run_outcomes(started, after), after)


def no_progress_from(rows: Sequence[Row], outcomes: Mapping[str, Sequence[RunOutcome]], after: int) -> dict[str, NoProgress]:
    """`chair_no_progress.no_progress_facts` with each initiative's fresh-start mark read off its queue row, so the runs
    up to the last `cox route release` do not count."""
    from core.workstore import fresh_cutoff  # lazy: core sits beside agent_tools in one venv

    cutoffs = {
        str(r.get("initiative") or r.get("task_id")): fresh_cutoff(extra if is_mapping(extra := r.get("extra")) else None)
        for r in rows
        if r.get("kind") == "initiative"
    }
    return no_progress_facts(outcomes, cutoffs, after)


def resolve_quarantine_hold_after(value: object) -> int:
    """The profile's raw `chair.quarantine_hold_after`: a whole number of at least 0 (0 never holds on the count), else 3.
    A bool, a fraction, a negative number and text all read as the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return DEFAULT_QUARANTINE_HOLD_AFTER
    return int(value) if value >= 0 and value == int(value) else DEFAULT_QUARANTINE_HOLD_AFTER


def held_tasks_from_rows(rows: Sequence[Row]) -> list[HeldTask]:
    """The task rows a person must decide on, in row order: `blocked` with a hold reason that carries the mark."""
    from core.workstore import HOLD_REASON, is_held  # lazy: core sits beside agent_tools in one venv

    return [
        {
            "initiative": str(r.get("initiative") or ""),
            "task_id": str(r.get("task_id") or ""),
            "phase": str(r.get("phase") or ""),
            "reason": str(extra_value(r, HOLD_REASON) or ""),
        }
        for r in rows
        if r.get("kind") == "task" and is_held(r)
    ]


def resolve_shared_file_limit(value: object) -> int:
    """A finite number of at least 1 (a bool is not a number) as an int, else the default. inf and nan parse from a profile."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value < math.inf:
        return DEFAULT_SHARED_FILE_LIMIT
    return int(value)


def resolve_serial_files(value: object) -> list[str]:
    """The string entries of a list; anything else is no serial paths."""
    return [entry for entry in value if isinstance(entry, str)] if is_list(value) else []


def resolve_idle_stall_minutes(value: object) -> int:
    """The profile's raw `chair.idle_stall_minutes`, or 15 when it is missing, non-numeric, or not positive."""
    try:
        minutes = int_of(value)
    except (TypeError, ValueError):
        return IDLE_STALL_MINUTES
    return minutes if minutes > 0 else IDLE_STALL_MINUTES


def steward_asked_for(value: object) -> bool:
    """Whether the profile still sets `chair.steward` on: boolean True or the string "true". It turns nothing on: the
    epic steward is gone, and the lander merges built phases and lands initiatives (`cox lander run`). The doctor
    says so when the key is still set."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


_VIEW_STATES: dict[str, TaskState] = {
    "ready": "ready", "approved": "approved", "done": "done", "quarantined": "quarantined", "dropped": "dropped", "building": "building",
}


def chat_epics(rows: Sequence[Row], run_exited: Mapping[str, bool]) -> list[EpicView]:
    """Pure. One view per initiative with a task that is not done or dropped, for the chair's answers to a session.

    A level is a phase of its task rows, in natural phase order. A store state outside the view's words (todo,
    blocked, in_progress) is open work, so it reads `building`."""
    tasks = [r for r in rows if r.get("kind") == "task"]

    def natural(phase: str) -> list[object]:
        return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", phase)]

    def view(row: Row) -> ViewTask:
        return {"id": str(row["task_id"]), "state": _VIEW_STATES.get(str(row["state"]), "building")}

    def build(initiative: str) -> EpicView:
        own = [r for r in tasks if r["initiative"] == initiative]
        phases = sorted({str(r["phase"]) for r in own}, key=natural)
        levels: list[ViewLevel] = [{"phase": p, "tasks": [view(r) for r in own if str(r["phase"]) == p]} for p in phases]
        return {"epic_id": initiative, "levels": levels, "exited": bool(run_exited.get(initiative, False))}

    open_ids = sorted({str(r["initiative"]) for r in tasks if r["state"] not in ("done", "dropped")})
    return [build(i) for i in open_ids]


def idle_stall_inputs(
    backlog: IdleBacklog,
    hosts: Sequence[HostCheck],
    lands_waiting: Sequence[LandWait],
    open_item: tuple[str | None, str | None],
    stall_minutes: int,
) -> IdleStallInputs:
    """The four idle readers' outputs as one input. `open_item` is `read_idle_open`'s (signature, diagnosis)."""
    return {
        "free_lanes": backlog["free_lanes"],
        "ready": backlog["ready"],
        "queued": backlog["queued"],
        "last_progress_at": backlog["last_progress_at"],
        "stall_minutes": stall_minutes,
        "hosts": list(hosts),
        "empty_stubs": list(backlog["empty_stubs"]),
        "lands_waiting": list(lands_waiting),
        "blocked_ready": list(backlog["blocked_ready"]),
        "open_signature": open_item[0],
        "open_diagnosis": open_item[1],
    }


def _smoke_of(watch: LandWatch, hold: HoldRecord | None) -> tuple[Literal["failed", "ok"], str]:
    """Smoke is failed when the hold names this commit, else ok; pending is never produced because no input says a smoke has not run."""
    if hold is not None and hold["land"]["commit"] == watch["commit"]:
        return "failed", hold["tail"]
    return "ok", ""


def landed_main_facts(
    watches: Sequence[LandWatch],
    read_main_ci: Callable[[str, str], tuple[str, str]],
    hold: HoldRecord | None,
    green_since: Callable[[str, str], bool] = lambda repo, commit: False,
) -> list[LandedMain]:
    """One LandedMain per watch: main's first CI state and output, the smoke verdict, and green_after, asked only of a red, infra or smoke-failed land."""

    def build(watch: LandWatch) -> LandedMain:
        ci, ci_output = read_main_ci(watch["repo"], watch["commit"])
        smoke, smoke_output = _smoke_of(watch, hold)
        broken = ci in ("red", "infra") or smoke == "failed"
        green_after = green_since(watch["repo"], watch["commit"]) if broken else False
        # `read_main_ci` answers a string, one of the four states `LandedMain.ci` names (`chair_revert_ports.main_ci`), passed through as read.
        return {
            **watch, "ci": ci, "ci_output": ci_output, "smoke": smoke, "smoke_output": smoke_output, "green_after": green_after,
        }

    return [build(w) for w in watches]


def gather_facts(
    deps: FactsDeps, now: datetime, epic_definitions: Mapping[str, Mapping[str, object]] | None = None
) -> Facts:
    """`epic_definitions` maps an epic id to its definition (title, target_branch); absent means none are known."""
    policy = deps.policy()
    weekly = deps.weekly()
    month = deps.month()
    assessment = pacing.assess(deps.window(), policy, now, weekly, month=month)
    docket = deps.docket()
    # Read once: `rows` drives ready, intake, quarantined and (with stranded_records) stranded below. An empty
    # read means the store has no queue table or harness yet, not that the board is empty, so each fact falls
    # back to its file reader.
    rows = list(deps.queue()) if deps.queue is not None else []
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    items = list(deps.tickets()) if deps.tickets is not None else []
    # Read once: the merged flags below and the stall alarm's land facts both read the landing records.
    landing_records = list(deps.landing_records()) if deps.landing_records is not None else []
    if deps.landing_records is not None and items:
        # The one place the merged flag takes its landing-model meaning for the tick: everything below reads `items`.
        item_land_at = land_at_facts(deps.cartridge(), deps.land_at_values(), sorted({str(i["initiative"]) for i in items}))
        items = with_landing_merged(items, landing_records, item_land_at)
    # `items` carry the store's `updated_at`, which the queue rows lack, so the docket's age tie-break holds here too.
    # Each initiative is judged by its own land_at, as `docket_by_land_at` does for the file docket.
    ready = (
        docket_from_rows(
            rows, now_iso, items,
            land_at_facts(deps.cartridge(), deps.land_at_values(), sorted({str(r["initiative"]) for r in rows if r["kind"] == "task"})),
        )
        if rows else docket["initiatives"]
    )
    dispatch = deps.dispatch(docket) if deps.dispatch else dispatch_facts(docket, [], {"": int(docket["busy_lanes"])})
    live = set(deps.live_initiatives())
    approved_rows = deps.approved()
    approved = approved_facts(
        approved_rows,
        land_at_facts(deps.cartridge(), deps.land_at_values(), sorted({str(r["initiative"]) for r in approved_rows})),
        deps.approved_branch_merged,
    )
    repos = dict(deps.repos()) if deps.repos is not None else {}
    ready_ids = {i["id"]: {t["id"] for t in i["ready_tasks"]} for i in ready}
    problems = dag_problems(items) if deps.tickets is not None else {}
    live_phase_pairs = [(p[0], p[1]) for p in deps.live_phases()] if deps.live_phases is not None else []
    leased = {(str(r["initiative"]), str(r["task_id"])) for r in rows if r["kind"] == "task" and chair_read_docket.is_claimed(r, now_iso)}
    initiatives: list[InitiativeFacts] = [
        {**i, "dag_problem": problems[i["id"]]} if i["id"] in problems else i
        for i in initiative_facts(
            {**docket, "initiatives": ready}, live,
            ticket_surfaces(items, ready_ids) if deps.tickets is not None else None,
            repos if deps.repos is not None else None,
            land_at_facts(deps.cartridge(), deps.land_at_values(), [i["id"] for i in ready]),
            {i["id"]: [t for t in items if t["initiative"] == i["id"]] for i in ready} if deps.tickets is not None else None,
            leased,
            live_phase_pairs,
        )
    ]
    newest_run_host = dict(deps.newest_run_host()) if deps.newest_run_host is not None else {}
    # An approved row is, by construction, an approved task not yet landed; `chair_plan_prune.phases_to_carry`
    # draws its carried-partial-phase set from these same rows, so their initiatives cover both cases home tracks.
    unfinished = {r["initiative"] for r in approved}
    attempts = list(deps.attempts())
    unfetched: Mapping[str, str] = deps.remote_unfetched() if deps.remote_unfetched is not None else {}
    # A queue row names no attempt under work_state store: its run and reason come from the attempts the reader found,
    # which are the runs' task records. A remote run's records come home with its fetch, so a quarantine older than its
    # initiative's unfetched run waits for that fetch rather than act on an older record, and says so on the status line.
    queued = with_attempt_runs(quarantined_from_rows(rows), attempts) if rows else []
    waiting = awaiting_fetch(queued, unfetched)
    held_back = {(w["initiative"], w["task_id"]) for w in waiting}
    quarantined = [q for q in queued if (q["initiative"], q["task"]) not in held_back] if rows else list(deps.quarantined())
    stranded_records = deps.stranded_records() if deps.stranded_records is not None else None
    stranded = stranded_from_rows(rows, list(stranded_records)) if rows and stranded_records is not None else list(deps.stranded())
    current = deps.current_version() if rows else None  # file mode carries no gate rows, so it reads no version
    intake = intake_paths_from_rows(rows, current) if rows else list(deps.intake())
    # Store mode drops stranded rows for tasks the store does not hold approved (see `store_approved_tasks`, which
    # also names the gap). File mode carries no store state and filters nothing.
    quarantine = quarantine_facts(
        quarantined, stranded, attempts, live, deps.has_patch, planned_tasks(approved, initiatives),
        store_approved_tasks(rows) if rows else None,
        waiting={(a["initiative"], a["id"]) for a in approved},
    )
    action_rows = list(deps.actions()) if deps.actions is not None else []
    index = action_index(action_rows)
    login_hosts: list[Mapping[str, object]] = [*deps.hosts()]
    decomposed = dict(deps.decomposed_intake()) if deps.decomposed_intake is not None else {}
    probe = dict(deps.pid_probe())
    from agent_tools.chair_decompose_runs import decompose_refusals  # lazy: chair_decompose_runs imports this module

    refusals = land_refusals_from_actions(selected(index, "land_phase", "partial_land"), now)
    work = list(deps.work_rows()) if deps.work_rows is not None else []
    epics = epic_facts(work, epic_definitions or {}) if deps.work_rows is not None else []
    facts: Facts = {
        "lease": lease_facts(deps.lease(), deps.session, deps.pid, deps.host),
        "limits": limits_facts(
            assessment, policy, weekly, fleet_lanes(dispatch), deps.window_source(), deps.weekly_source(),
            deps.runs_dir(), now=now, month=month,
        ),
        "dispatch": dispatch,
        "approved": approved,
        "home": initiative_homes(newest_run_host, unfinished, deps.host),
        "initiatives": initiatives,
        "quarantines": quarantine,
        "intake": intake,
        "work_store_ready": deps.work_store_ready(),
        "sources_configured": deps.sources_configured(),
        "profile_stale": deps.profile_stale(),
        "drafts": deps.drafts() if deps.drafts is not None else 0,
        "missing_repos": new_missing_repos(
            deps.missing_repos() if deps.missing_repos is not None else [],
            deps.reported_repos() if deps.reported_repos is not None else set(),
        ),
        "handled_keys": list(deps.handled_keys()) if deps.handled_keys is not None else [],
        "run_exited": dict(deps.run_exited()) if deps.run_exited is not None else {},
        "remote_unfetched": dict(unfetched),
        "awaiting_fetch": waiting,
        "remote_unfetched_decompose": list(unfetched.decompose) if isinstance(unfetched, RemoteUnfetched) else [],
        "schema_deaths": {i: list(r) for i, r in deps.schema_deaths().items()},
        "lost_runs": dict(deps.lost_runs()) if deps.lost_runs is not None else {},
        "pid_probe": probe,
        "empty_decompose": empty_decompose_facts(
            decomposed,
            live,
            dict(deps.item_counts()) if deps.item_counts is not None else {},
            dict(deps.initiative_states()) if deps.initiative_states is not None else {},
            alive=alive_decompose_runs(deps.runs_dir(), decomposed, probe),
            remote_pending=remote_pending_decompose_runs(deps.runs_dir(), decomposed, now),
            refusals=decompose_refusals(Path(deps.runs_dir()), decomposed.values()),
        ),
        "todo_initiatives": todo_initiative_facts(list(deps.work_rows())) if deps.work_rows is not None else [],
        "decompose_streaks": decompose_streak_facts(intake, deps.decompose_runs, deps.decompose_cutoff) if deps.decompose_runs is not None else {},
        "decompose_stalled_reported": decompose_stalled_keys(selected(index, ("needs_chair", "decompose_stalled"))),
        "env_stale_reported": stale_checkout_keys(selected(index, ("needs_chair", STALE_CHECKOUT_CAUSE))),
        "held_files": [HeldFile(**h) for h in deps.held_files()],
        "held_reported": held_file_keys(selected(index, ("needs_chair", HELD_FILE_CAUSE))),
        "stale_drafts_reported": stale_draft_keys(selected(index, "stale_to_draft")),
        "held_initiatives": sorted(held_initiatives(rows)) if rows else [],
        "deferred_initiatives": deferred_initiatives(rows, current) if rows and current is not None else {},
        "host_env": host_env_from_rows(login_hosts),
        "relaunch_loop_reported": relaunch_loop_keys(selected(index, ("needs_chair", "relaunch_loop"))),
        "land_refusals": refusals,
        "land_conflicts": read_land_conflicts(deps.runs_dir(), repos),
        "land_heads": read_land_heads(refusals, repos, deps.rev_parse),
        "launch_history": launch_history_from_actions(selected(index, "launch_epic", "relaunch"), quarantine, now),
        "lease_refusals": lease_refusals_from_actions(selected(index, "launch_epic", "relaunch"), now),
        "max_launches_per_hour": resolve_max_launches_per_hour(
            deps.max_launches_per_hour() if deps.max_launches_per_hour is not None else None
        ),
        "quarantine_hold_after": resolve_quarantine_hold_after(
            deps.quarantine_hold_after() if deps.quarantine_hold_after is not None else None
        ),
        "held_tasks": held_tasks_from_rows(rows),
        "no_progress": _no_progress(deps, rows, [i["id"] for i in initiatives if i.get("started")]),
        "newest_run_host": newest_run_host,
        "run_hosts": run_host_facts(deps.run_hosts()),
        "login_hosts": login_hosts,
        "last_housekeeping_at": deps.history() if deps.history is not None else None,
        "housekeeping_hours": resolve_housekeeping_hours(deps.housekeeping_hours())
        if deps.housekeeping_hours is not None
        else DEFAULT_HOUSEKEEPING_HOURS,
        "stale_candidates": list(deps.stale_candidates(now)) if deps.stale_candidates is not None else [],
        "stall_candidates": list(deps.stall_candidates(now)) if deps.stall_candidates is not None else [],
        "stale_days": resolve_stale_days(deps.stale_days()) if deps.stale_days is not None else DEFAULT_STALE_DAYS,
        "review_prs": list(deps.review_prs()),
        "phase_tasks": list(deps.phase_tasks()),
        "running": (
            running_initiatives(live, items, repos, unlanded=unfinished, live_phases=live_phase_pairs)
            if deps.tickets is not None
            else []
        ),
        "steer_streaks": steer_streaks_from_actions(selected(index, "steer_clear", *sorted(LAUNCH_KINDS))),
        "land_fail_streaks": land_fail_streaks_from_actions(selected(index, *LAND_FAIL_KINDS)),
        "no_run_streaks": no_run_streaks_from_actions(
            approved, selected(index, "requeue_task", *sorted(HOLD_LAND_KINDS), ("needs_chair", "no_run"))
        ),
        "approval_holds": approval_holds_from_actions(approved, selected(index, *sorted(HOLD_KINDS))),
        "wrongly_released": list(deps.wrongly_released()),
        "steer_policy": {
            "limit": resolve_shared_file_limit(deps.shared_file_limit() if deps.shared_file_limit is not None else None),
            "serial": resolve_serial_files(deps.serial_files() if deps.serial_files is not None else None),
        },
        "waiting_reported": waiting_reported_from_actions(selected(index, "waiting", *sorted(LAUNCH_KINDS))),
        "pull_age_s": pull_age_s(selected(index, "pull"), now),
        "stranded": stranded_facts(approved, list(deps.run_commits()), list(deps.phase_state())),
        "phase_branches": phase_branch_facts(list(deps.branch_counts())),
        "landing": list(deps.landing()) if deps.landing is not None else [],
        "idle_stall": deps.idle_stall(now),
        "stall_alarm": stall_facts(
            resolve_settings(deps.stall_alarm() if deps.stall_alarm is not None else None),
            landing_records, [action_doc(row) for row in action_rows],
        ),
        "tuning": tuning_facts(
            *deps.tuning(now),
            weekly_fraction(weekly),
            pacing.window_elapsed_fraction(weekly, now) if weekly is not None else 0.0,
            current_lanes(dispatch), dict(deps.lane_bounds()),
        ),
        "landed_main": landed_main_facts(list(deps.land_watches()), deps.read_main_ci, deps.land_hold(), deps.green_since),
        "land_outcomes": dict(deps.land_outcomes()),
        "handoff_stops": list(deps.handoff_stops()),
        "queue_files": deps.queue_files(),
        "epics": epics,
    }
    # The keys below ride along only when their source was read (or, for the freeze, set): see each key's note on `Facts`.
    misses = decompose_miss_fact_keys(
        selected(index, "launch_decompose", ("needs_chair", MISSING_IDEA_CAUSE)), deps.intake_fingerprint, frozenset(intake)
    )
    facts["decompose_misses"] = misses["decompose_misses"]
    facts["decompose_miss_fingerprints"] = misses["decompose_miss_fingerprints"]
    facts["decompose_misses_reported"] = misses["decompose_misses_reported"]
    if (freeze := land_freeze_fact(deps.runs_dir(), now)) is not None:
        facts["land_freeze"] = freeze
    if (queue := merge_queue_fact(deps.merge_queue() if deps.merge_queue is not None else None)) is not None:
        facts["merge_queue"] = queue
    if (ceiling := resolve_load_ceiling(deps.host_load_ceiling() if deps.host_load_ceiling is not None else None)) is not None:
        facts["host_load_ceiling"] = ceiling
    if deps.phase_records is not None:
        facts["phase_records"] = list(deps.phase_records(approved))
    if deps.live_phases is not None:
        facts["live_phases"] = [LivePhase(initiative=i, phase=p) for i, p in live_phase_pairs]
    if deps.tickets is not None:
        facts["schedule_inputs"] = schedule_inputs_from_rows(
            items, work, selected(index, "launch_epic", "relaunch", "land_phase"), dispatch,
            local=deps.host, repos=repos,
            land_at=land_at_facts(deps.cartridge(), deps.land_at_values(), sorted({str(i["initiative"]) for i in items})),
            live_phases=live_phase_pairs,
            newest_run_host=newest_run_host,
        )
    if deps.work_rows is not None:
        facts["chat_epics"] = chat_epics(work, dict(deps.run_exited()) if deps.run_exited is not None else {})
    return facts
