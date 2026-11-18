"""The chair's facts, the reads: the deps, queue, lease, limits, quarantine, phase and schedule facts."""

import json
import re
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from agent_tools import chair_smoke, forge, pacing, queue_rows
from agent_tools.budget import MonthBudget
from agent_tools.chair import lease_holder
from agent_tools.chair_decompose_streak import DecomposeRun
from agent_tools.chair_land_freeze import LandFreeze, read_freeze
from agent_tools.chair_no_progress import RunOutcome
from agent_tools.chair_phase_quarantine import marked_cause
from agent_tools.chair_phases import launchable_phases
from agent_tools.chair_read_land_conflicts import _rev_parse
from agent_tools.chair_schedule_durations import END_KEY, MIX_KEY, SIZE_KEY, START_KEY
from agent_tools.chair_types import (
    EASTERN,
    QUARANTINED_CAUSE,
    ApprovedTask,
    AwaitingFetch,
    DispatchFacts,
    HeldFile,
    HoldRecord,
    IdleStallInputs,
    LandWatch,
    LeaseFacts,
    LimitsFacts,
    MergeQueueFacts,
    PhaseFacts,
    PhaseTasks,
    PidProbeFact,
    QuarantineFacts,
    QueueFile,
    ReviewPr,
    RunningInitiative,
    ScheduleHost,
    ScheduleInitiative,
    ScheduleInputs,
    SchedulePhase,
    ScheduleRunning,
    WronglyReleased,
)
from agent_tools.land_batch_settings import merge_queue_setting

HARNESS_CAUSE = "harness"
RESCUE_FAILED_CAUSE = "rescue_failed"
STRANDED_CAUSE = "stranded"
LAUNCHING_VERDICTS = frozenset({"go", "go_degraded"})
LAUNCH_KINDS = frozenset({"launch_epic", "relaunch", "retry", "rescue"})
STEER_STATES = frozenset({"ready", "approved"})

Row = Mapping[str, Any]


def _package_version() -> str:
    """Edge. The umbrella's installed version via `version_info.package_version`; imported late to keep it off this module's import path."""
    from agent_tools.version_info import package_version

    return package_version()


@dataclass(frozen=True)
class FactsDeps:
    """One callable per source, so a test passes fakes. Each field names the row keys its source returns.

    lease: the store lease record, keys holder, host, epoch, released, stale.
    docket: the `route context` docket, keys initiatives (id, started, ready_tasks, landed), busy_lanes, max_in_flight.
    approved: approved tasks, keys id, initiative, repo, phase, phase_done, needs, run (empty when no record is stranded).
    quarantined: open quarantined work items, keys initiative, phase, task, from each item's work-store path.
    stranded: `runs_stranded.stranded` rows as they land, keys run, task, phase, branch, remedy, expected.
        No initiative. `expected` is true for a deliberate `land: manual` repo, whose row raises no quarantine entry.
    attempts: every attempt over all runs, oldest first, keys run, phase, task, cause, and initiative when known.
        A work item's frontmatter `attempts` entries carry run and cause, as `stats_chair` reads them; the item's
        path adds phase and task. Under work_state store no work file holds an attempt, so the run task records
        under `runs/` add one row per record that names a cause, for each task the store holds open
        (`chair_read_run_records`); `gather_facts` gives a queue quarantine its run and reason from these rows.
    has_patch: (initiative, task id) to whether the task's stored record holds a non-blank build.patch. Called once
        per quarantine entry, and False when no record can be found.
    live_initiatives: initiatives with a live run, meaning the `runs:<initiative>` store lease is held and
        unexpired, or failing that `runs/<run>.pid` names a live pid.
    drafts: the number of initiatives waiting for approval. Optional, and absent means 0.
    missing_repos: sorted, de-duplicated repository path strings, the list `runs_stranded` will produce for
        repositories that no longer exist. Optional, and absent means empty.
    reported_repos: the paths already reported on an earlier tick. Optional, and absent means empty.
        A missing repository is reported once, in the status line, and never becomes a needs_chair fact.
    remote_unfetched: initiative id to run id, for each initiative whose newest run carries a remote
        record and no local run directory or log. Optional, and absent means no initiative counts.
    schema_deaths: initiative id to its newest two run ids, for each initiative whose newest two runs both
        exited on the schema-version refusal. Defaults to a callable returning {}.
    lost_runs: initiative id to run id, for each lane whose host has gone stale (no heartbeat for the
        threshold) with no exit record in the store. Optional, and absent means no initiative counts.
    pid_probe: initiative id to `{alive, last_beat_at}`, for a remote run whose host has a fresh beat and
        whose pid was probed. A stale host, or a probe that failed or timed out, has no entry. Defaults to
        a callable returning {}.
    decomposed_intake: initiative id to its most recent decompose run id, for every initiative whose intake
        status is decomposed. Optional, and absent means no initiative counts.
    item_counts: initiative id to the count of task items the work store holds for it. Optional, and absent
        means every initiative counts as non-empty, so none is reported as empty-decompose.
    initiative_states: initiative id to its store state. Optional, and absent means no state is known, so
        none is left out of empty-decompose as done or dropped.
    work_rows: every row of the store's work table with its `kind`, as `run_store.read_queue` returns them. Shaped
        by `todo_initiative_facts` into the `todo_initiatives` fact. Optional, and absent means no initiative counts.
    newest_run_host: initiative id to its newest run's host, "" meaning the local machine
        (`run_store.newest_run_hosts`). Optional, and absent means no initiative counts.
    run_hosts: run id to the host name that ran it, "" meaning the local machine. Defaults to a callable
        returning {}, so no run counts until the edge wires it. `run_host_facts` drops the local entries, so the `run_hosts` fact holds remote runs only.
    hosts: the same seam as `lost_runs`, `run_store.hosts(runs_dir)`'s raw rows from the store's `hosts`
        table, one per host, keys name, state, versions_json, and whatever else that table carries. Stored
        verbatim under the `login_hosts` fact for the login watch to read. Defaults to a callable returning [].
    history: the newest housekeeping action's ISO timestamp (`chair_read_housekeeping.read_last_housekeeping`).
        Optional, and absent means no history, i.e. housekeeping is due.
    housekeeping_hours: the raw profile value at `chair.housekeeping_hours`, resolved by `resolve_housekeeping_hours`.
        Optional, and absent means 24 hours.
    max_launches_per_hour: the raw profile value at `chair.max_launches_per_hour`, resolved by
        `resolve_max_launches_per_hour`. Optional, and absent means the default.
    quarantine_hold_after: the raw profile value at `chair.quarantine_hold_after`, resolved by
        `resolve_quarantine_hold_after`. Optional, and absent means 3.
    shared_file_limit: the raw profile value at `chair.max_shared_file_initiatives`, resolved by
        `resolve_shared_file_limit`. Optional, and absent means 2.
    serial_files: the raw profile value at the top-level `serial_files`, resolved by `resolve_serial_files`.
        Optional, and absent means no serial paths.
    host_load_ceiling: the raw profile value at `chair.host_load_ceiling`, resolved by `host_load.resolve_load_ceiling`.
        Optional, and absent means no ceiling.
    merge_queue: the raw profile value at the top-level `merge_queue`, resolved by `merge_queue_fact`. Optional, and
        absent means the queue is off.
    stale_days: the raw profile value at `chair.stale_days`, resolved by `resolve_stale_days`. Optional, and
        absent means 7 days.
    stale_candidates: `stale_candidates(now) -> list[dict]`, bound in production to
        `chair_read_stale.read_stale_candidates`. Optional, and absent means no candidates.
    stall_candidates: `stall_candidates(now) -> list[dict]`, bound in production to
        `chair_read_stall.read_stall_candidates`. Optional, and absent means no candidates.
    idle_stall: `idle_stall(now) -> IdleStallInputs`, bound in production to the four `chair_read_idle_*` readers
        (hosts, lands, backlog, open item) plus the configured `stall_minutes`. Stored unchanged under the
        `idle_stall` fact. Required, so an unwired detector fails at construction instead of planning nothing.
        `idle_stall_inputs` assembles the readers' outputs and `resolve_idle_stall_minutes` the threshold.
    queue: `run_store.read_queue`'s rows for this tick, read once and fed to `chair_read_docket.docket_from_rows`,
        `chair_read_intake.intake_from_rows`, `chair_read_quarantined.quarantined_from_rows` and
        `stranded_records`-paired `chair_read_stranded.stranded_from_rows` to build ready, intake, quarantined and
        stranded facts from the store. Optional, and an absent callable or an empty read falls the four facts back
        to `docket`, `intake`, `quarantined` and `stranded`: an empty read means the store has no queue table or
        harness yet, not that the board is empty.
    stranded_records: the raw per-run task records `chair_read_stranded.stranded_from_rows` pairs against `queue`'s
        task rows, the shape `_chair_stranded_inputs` reads from each run's `tasks/*/*.json` file. Optional, and
        absent leaves `stranded` on the file reader even when `queue` has rows.
    window_source: "meter" when `window` built this tick's figure from a fresh status-line meter entry, else
        "est" for the estimate. Judged from the same per-tick meter read as `window`, so the label cannot
        disagree with the figure. Optional, and absent means "est".
    weekly_source: the same reading as `window_source`, for `weekly`. Optional, and absent means "est".
    month: the month's `budget.MonthBudget`, or None when the profile sets no monthly budget. Optional, and absent
        means None. `gather_facts` passes it to `pacing.assess` and `limits_facts` ORs its fraction into `hard_stop`.
    runs_dir: the runs directory `limits_facts` reads `chair.hold.json` from, via `chair_smoke.read_hold`.
        Optional, and absent means "", so no hold is ever read and `limits.smoke_hold` stays None.
    review_prs: the approved tasks awaiting a review PR, each with its forge state, as `forge_review_prs` builds
        them in production. Optional, and absent means an empty list.
    tickets: every work item under `work/<initiative>/<phase>/<task>.md`, keys initiative, id, state, surfaces
        (`read_ticket_items`). Optional, and absent means no initiative has surfaces and `running` is empty.
        The store queue is not bound in the chair, so surfaces are never read from `queue`.
    repos: initiative id to the `repo:` of its `initiative.md` (`read_initiative_repos`). Optional, and absent
        means no initiative has a known repo.
    actions: the `chair_actions` rows, keys kind, ts, and action_json or the action's own keys
        (`chair_read_stale.read_chair_actions`). `steer_streaks`, `decompose_stalled_reported`, `land_refusals`,
        `launch_history` and `relaunch_loop_reported` derive from them, and so do `decompose_misses` (each intake
        path's run of decompose launches that found no idea file) and `decompose_misses_reported`.
        Build it with `windowed_actions` so one gather reads the table once and every caller shares the rows.
        Optional, and absent means {}.
    intake_fingerprint: an intake path to a digest that changes when the intake does (`chair_read_decompose_miss`),
        read for each path in `decompose_misses` still in `intake` into `decompose_miss_fingerprints`; a path no longer
        queued drops out. It returns None when it cannot be read this tick, and the recorded fingerprint then stands.
        Optional, and absent means both are {}, so no intake is ever held back for a missing idea file.
    run_commits: one row per approved commit a run holds, oldest first, keys task, initiative, phase, run, host,
        branch (the run's agents branch), commit, run_seq. Optional, and absent means no stranded phase is built.
    phase_state: one row per phase with approved work, keys initiative, phase, tasks (every task id of the phase),
        adds (whether landing the phase would add commits over main). Optional, and absent means no stranded phase.
    branch_counts: one row per initiative with a phase branch, keys initiative, phase, branch, ahead, behind, tip,
        counted against main by the git reader. Optional, and absent means no phase branch.
    landing: the lands in progress on the land worker, keys initiative, phase, repo (`WorkerLands.landing`).
        Optional, and absent means an empty list.
    land_watches: the lands not yet resolved, as `LandWatch` rows. Defaults to a callable returning [].
    handoff_stops: the ready or blocked tickets with an attempt on their current body, as `HandoffStop` rows
        (`chair_read_handoff_stops.read_handoff_stops`). Defaults to a callable returning [].
    read_main_ci: (repo, commit) to (state, output) of main's first CI run for that commit, state pending, green,
        red or infra. Defaults to a callable returning pending.
    green_since: (repo, commit) to whether a green main run exists at or after that commit. Consulted only for a red,
        infra or smoke-failed land. Defaults to a callable returning False.
    land_hold: the smoke hold, `chair_smoke.read_hold`'s result. Defaults to a callable returning None.
    land_outcomes: initiative to its resolved land outcomes, oldest first. Defaults to a callable returning {}.
    approved_branch_merged: an approved row to whether its commit is already on its phase or pr branch, bound in
        production to `chair_read_branch_merged.branch_merged`. Not consulted under task landing. Defaults to a
        callable returning False.
    wrongly_released: the ready tasks the approval hold released while their commit was on a phase or pr branch,
        built from `wrongly_released_facts` in production. Defaults to a callable returning [].
    """

    lease: Callable[[], Row]
    window: Callable[[], pacing.Window]
    weekly: Callable[[], pacing.Window | None]
    policy: Callable[[], pacing.Policy]
    docket: Callable[[], Row]
    approved: Callable[[], Sequence[Row]]
    quarantined: Callable[[], Sequence[Row]]
    stranded: Callable[[], Sequence[Row]]
    attempts: Callable[[], Sequence[Row]]
    has_patch: Callable[[str, str], bool]
    live_initiatives: Callable[[], Collection[str]]
    intake: Callable[[], Sequence[str]]
    work_store_ready: Callable[[], bool]
    sources_configured: Callable[[], bool]
    session: str
    pid: int
    host: str
    idle_stall: Callable[[datetime], IdleStallInputs]  # now -> the idle-lanes stall inputs, stored unchanged
    dispatch: Callable[[Row], DispatchFacts] | None = None  # docket -> lane facts; absent counts every busy lane as local
    queue: Callable[[], Sequence[Row]] | None = None  # run_store.read_queue rows; absent or empty falls back to docket, intake, quarantined and stranded
    stranded_records: Callable[[], Sequence[Row]] | None = None  # paired with queue's task rows to build stranded; absent leaves stranded on the file reader
    drafts: Callable[[], int] | None = None
    missing_repos: Callable[[], list[str]] | None = None
    reported_repos: Callable[[], set[str]] | None = None
    handled_keys: Callable[[], Sequence[str]] | None = None  # the sort's keys already acted on; None means none
    run_exited: Callable[[], Mapping[str, bool]] | None = None  # initiative to whether its newest run has an exit record; absent means {}
    remote_unfetched: Callable[[], Mapping[str, str]] | None = None  # a `RemoteUnfetched` also carries the decompose-only initiatives
    lost_runs: Callable[[], Mapping[str, str]] | None = None  # initiative to run id, for a lane whose host is stale and whose run has no exit record; absent means {}
    decomposed_intake: Callable[[], Mapping[str, str]] | None = None  # initiative to its newest decompose run id, for a decomposed intake; absent means {}
    item_counts: Callable[[], Mapping[str, int]] | None = None  # initiative to its stored task-item count; absent means every initiative counts as non-empty
    initiative_states: Callable[[], Mapping[str, str]] | None = None  # initiative to its store state; absent means {}, so none is skipped as done or dropped
    work_rows: Callable[[], Sequence[Row]] | None = None  # every store work row with its `kind` (run_store.read_queue); absent means no todo initiatives
    newest_run_host: Callable[[], Mapping[str, str]] | None = None  # initiative to its newest run's host, "" meaning local; absent means {}
    run_hosts: Callable[[], Mapping[str, str]] = lambda: {}  # run id to the host that ran it, "" meaning local; absent means {}
    schema_deaths: Callable[[], Mapping[str, list[str]]] = lambda: {}  # initiative to its newest two run ids when both died on the schema refusal; absent means {}
    history: Callable[[], str | None] | None = None  # chair_read_housekeeping.read_last_housekeeping; absent means no history
    housekeeping_hours: Callable[[], object] | None = None  # raw profile chair.housekeeping_hours; absent means 24 hours
    stale_days: Callable[[], object] | None = None  # raw profile chair.stale_days; absent means 7 days
    max_launches_per_hour: Callable[[], object] | None = None  # raw profile chair.max_launches_per_hour; absent means the default
    quarantine_hold_after: Callable[[], object] | None = None  # raw profile chair.quarantine_hold_after; absent means 3
    no_progress_after: Callable[[], object] | None = None  # raw profile chair.no_progress_after; absent means 2
    stall_alarm: Callable[[], object] | None = None  # raw profile chair.stall_alarm mapping; absent means every default
    # (initiative ids, how many) -> each initiative's newest ended runs, newest first (chair_read_run_outcomes); absent means no no-progress hold
    run_outcomes: Callable[[Sequence[str], int], Mapping[str, Sequence[RunOutcome]]] | None = None
    shared_file_limit: Callable[[], object] | None = None  # raw profile chair.max_shared_file_initiatives; absent means 2
    serial_files: Callable[[], object] | None = None  # raw profile top-level serial_files; absent means none
    host_load_ceiling: Callable[[], object] | None = None  # raw profile chair.host_load_ceiling; absent means no ceiling
    merge_queue: Callable[[], object] | None = None  # raw profile top-level merge_queue; absent means the queue is off
    stale_candidates: Callable[[datetime], Sequence[Row]] | None = None  # chair_read_stale.read_stale_candidates; absent means no candidates
    stall_candidates: Callable[[datetime], Sequence[Row]] | None = None  # chair_read_stall.read_stall_candidates; absent means no candidates
    hosts: Callable[[], list[dict]] = lambda: []  # run_store.hosts(runs_dir) rows, stored verbatim under login_hosts
    window_source: Callable[[], str] = lambda: "est"  # "meter" when `window` built from a fresh meter entry this tick
    weekly_source: Callable[[], str] = lambda: "est"  # "meter" when `weekly` built from a fresh meter entry this tick
    runs_dir: Callable[[], str] = lambda: ""  # the runs directory `limits_facts` reads chair.hold.json from; absent means no hold is ever read
    month: Callable[[], MonthBudget | None] = lambda: None  # this month's budget standing; None means the profile has no monthly budget
    review_prs: Callable[[], list[ReviewPr]] = lambda: []  # forge_review_prs bound to the profile's forge; absent means []
    pid_probe: Callable[[], Mapping[str, PidProbeFact]] = lambda: {}  # initiative to its remote pid probe; absent means {}
    tickets: Callable[[], Sequence[Row]] | None = None  # `read_ticket_items` rows; absent means no surfaces and no running initiatives
    # The store's landing records (`harness.landing_store.list_records`). With it, an approved task of an initiative
    # that lands at `initiative` carries the merged flag exactly when the record says its phase merged. Absent means
    # the flag is left as each reader found it.
    landing_records: Callable[[], Sequence[Mapping[str, Any]]] | None = None
    live_phases: Callable[[], Sequence[tuple[str, str]]] | None = None  # live (initiative, phase) runs; absent means no phase runs live
    repos: Callable[[], Mapping[str, str]] | None = None  # initiative to its initiative.md `repo:`; absent means no repo is known
    actions: Callable[[], Sequence[Row]] | None = None  # windowed chair_actions rows, read once per gather (`windowed_actions`); absent means no steer streaks
    decompose_runs: Callable[[str], Sequence[DecomposeRun]] | None = None  # intake path to its decompose runs, oldest first; absent means no decompose streaks
    decompose_cutoff: Callable[[str], datetime | None] | None = None  # intake path to its last release or requeue time; absent means every run counts
    intake_fingerprint: Callable[[str], str | None] | None = None  # intake path to its fingerprint now (`chair_read_decompose_miss`); absent means no decompose misses
    run_commits: Callable[[], Sequence[Row]] = lambda: []  # approved commits per run, oldest first; absent means []
    phase_state: Callable[[], Sequence[Row]] = lambda: []  # per phase: its task ids and whether a phase land adds over main; absent means []
    phase_tasks: Callable[[], list[PhaseTasks]] = lambda: []  # per phase with an approved task, each task's state; absent means []
    phase_records: Callable[[list[ApprovedTask]], list[str]] | None = None  # approved tasks to the `<run>:<phase>` of theirs with a phase record; absent means unknown
    branch_counts: Callable[[], Sequence[Row]] = lambda: []  # per phase branch: ahead, behind, tip against main; absent means []
    landing: Callable[[], Sequence[Row]] | None = None  # WorkerLands.landing; absent means []
    tuning: Callable[[datetime], tuple[Sequence[Row], str | None]] = lambda now: ([], None)  # chair_read_tuning.read_tuning bound to its db, runs dir and current tiers; absent means no rows and no last tune
    profile_stale: Callable[[], bool] = lambda: False  # read after sources_configured: this tick's profile re-read failed and the last good value was kept
    lane_bounds: Callable[[], Mapping[str, tuple[int, int]]] = lambda: {}  # host ("" local) to the cartridge's (min, max) lanes; absent means {}
    land_watches: Callable[[], Sequence[LandWatch]] = lambda: []  # unresolved lands; absent means landed_main is []
    read_main_ci: Callable[[str, str], tuple[str, str]] = lambda repo, commit: ("pending", "")  # (repo, commit) to (state, output)
    green_since: Callable[[str, str], bool] = lambda repo, commit: False  # (repo, commit) to whether main went green after; absent means False
    land_hold: Callable[[], HoldRecord | None] = lambda: None  # chair_smoke.read_hold's result; absent means no hold
    land_outcomes: Callable[[], Mapping[str, list[str]]] = lambda: {}  # initiative to resolved outcomes, oldest first; absent means {}
    handoff_stops: Callable[[], Sequence[Row]] = lambda: []  # chair_read_handoff_stops.read_handoff_stops rows; absent means []
    queue_files: Callable[[], list[QueueFile]] = lambda: []  # `read_queue_files` bound to the workspace and store; absent means []
    held_files: Callable[[], Sequence[HeldFile]] = lambda: []  # `queue_export.read_held_record` bound to the workspace; absent means []
    current_version: Callable[[], str] = _package_version  # the umbrella's current version, read once per tick; gates target_release
    rev_parse: Callable[[str, str], str | None] = _rev_parse  # (repo, ref) to the commit sha or None; read by `read_land_heads` for refused phases only
    cartridge: Callable[[], Mapping[str, Any]] = lambda: {}  # the resolved team cartridge; {} when unreadable, so land_at reads the default
    land_at_values: Callable[[], Mapping[str, str | None]] = lambda: {}  # initiative to its initiative.md `land_at:`; absent means {}
    approved_branch_merged: Callable[[Row], bool] = lambda row: False  # approved row to whether its commit is on a phase or pr branch
    wrongly_released: Callable[[], Sequence[WronglyReleased]] = lambda: []  # ready tasks released while merged on a branch; absent means []


def read_queue_files(workspace: Path, rows: Sequence[Row], stored: Mapping[tuple[str, str], str]) -> list[QueueFile]:
    """Edge. One entry per task row with content: its stored export hash and the text of its file, None when the file is absent."""
    from agent_tools.queue_export import is_contentless
    from agent_tools.queue_rows import row_path

    def entry(row: Row) -> QueueFile:
        path = Path(workspace) / row_path(row)
        key = (str(row.get("initiative") or ""), str(row.get("task_id") or ""))
        return {
            "task_id": key[1],
            "stored_hash": stored.get(key),
            "file_text": path.read_text() if path.is_file() else None,
        }

    return [entry(row) for row in rows if row.get("kind") == "task" and not is_contentless(row)]


def forge_review_prs(runs_dir: str, forge_name: str, resolve: Callable[[str], Any] = forge.forge_for) -> list[ReviewPr]:
    """Edge. The one place a PR's state is read, through the forge registry. A missing forge gives state unknown."""
    # Imported here because the reader imports `run_initiative` from this module.
    from agent_tools.chair_read_review_prs import read_review_prs

    module = resolve(forge_name)  # None for a missing forge, and `forge.pr_state` answers unknown for None
    return read_review_prs(Path(runs_dir), lambda url: forge.pr_state(module, url))


def land_freeze_fact(runs_dir: str, now: datetime) -> LandFreeze | None:
    """Edge. The live land freeze, or None. An empty `runs_dir` never touches the store; an expired row is no freeze."""
    return read_freeze(Path(runs_dir), now) if runs_dir else None


def merge_queue_fact(raw: object) -> MergeQueueFacts | None:
    """The enabled merge queue from the raw profile `merge_queue` value, or None when it is off, absent or malformed.

    wait_s is capped at the largest finite float. The settings keep a huge int or an inf as given, and `float()` of an int
    past that cap raises OverflowError, which would stop fact gathering for the whole chair.
    """
    queue = merge_queue_setting({"merge_queue": raw})
    return {"batch_size": queue.batch_size, "wait_s": float(min(queue.wait_s, sys.float_info.max))} if queue.enabled else None


def run_host_facts(raw: Mapping[str, str]) -> dict[str, str]:
    """Run id to host name for the runs that ran on a remote host; a local run (host "") is absent."""
    return {run: host for run, host in raw.items() if host}


def lease_facts(record: Row, session: str, pid: int, host: str) -> LeaseFacts:
    """`mine` needs this holder on a lease that is neither released nor stale."""
    holder = str(record.get("holder") or "")
    released = bool(record.get("released", False))
    stale = bool(record.get("stale", False))
    return cast(LeaseFacts, {
        "holder": holder,
        "host": str(record.get("host") or host),
        "epoch": int(record.get("epoch") or 0),
        "mine": holder == lease_holder(session, pid, host) and not released and not stale,
        "released": released,
        "stale": stale,
        "expired": bool(record.get("expired", False)),
        "until": str(record.get("until") or ""),
        "lent": bool(record.get("lent", False)),
        **({"session_held": bool(record["session_held"])} if "session_held" in record else {}),
    })


def weekly_fraction(weekly: pacing.Window | None) -> float:
    """Spent over ceiling. No weekly window, or one with no usable ceiling, is unguarded and reads 0.0, as in pacing.assess."""
    if weekly is None or weekly.ceiling_usd is None or weekly.ceiling_usd <= 0:
        return 0.0
    return weekly.spent_usd / weekly.ceiling_usd


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def window_start_day(start: datetime) -> str:
    """`Sat 23:00 EDT`: the start in EASTERN, as the status line prints its own time.

    `start` arrives in UTC, so formatting it unconverted shows the wrong hour and, near midnight, the wrong day.
    The day name comes from WEEKDAYS, not `%a`, which follows the host's locale."""
    local = start.astimezone(EASTERN)
    return f"{WEEKDAYS[local.weekday()]} {local:%H:%M %Z}"


def scaled_cap(max_in_flight: int, lane_fraction: float) -> int:
    """`max_in_flight` scaled by the lane fraction and rounded down. A positive fraction never rounds to 0 lanes."""
    if lane_fraction <= 0:
        return 0
    return max(1, int(max_in_flight * lane_fraction))


def fleet_lanes(dispatch: DispatchFacts) -> int:
    """The chair machine's `max_in_flight` plus each lane host's `capacity`, a host naming none counting
    `max_in_flight` as `host_free_slots` does. The launch cap is scaled from this: it bounds the whole fleet's launches."""
    own = int(dispatch["max_in_flight"])
    return own + sum(int(h.get("capacity", own)) for h in dispatch.get("hosts", []))


def limits_facts(
    assessment: pacing.Assessment, policy: pacing.Policy, weekly: pacing.Window | None, max_in_flight: int,
    window_source: str = "est", weekly_source: str = "est", runs_dir: str = "", now: datetime | None = None,
    month: MonthBudget | None = None,
) -> LimitsFacts:
    """`launch_cap` is the smaller of the 5-hour and weekly verdicts: none on a 5-hour `hold` or `stop`, otherwise
    `max_in_flight` (the fleet's lanes, `fleet_lanes`, at the edge) scaled by the weekly lane fraction. The weekly verdict never raises the cap.
    With no `now` the weekly pace is not judged and the lane fraction is 1.0.

    `smoke_hold` comes from `chair_smoke.read_hold(runs_dir)`, so a caller such as `chair_plan._launch_cap`
    or the status line sees a held post-land smoke failure, cause and all, without reading the filesystem
    itself. An empty `runs_dir` (the default) never touches the filesystem and reads as no hold.

    `hard_stop` is also true when `month`'s fraction reaches the weekly hard-stop fraction. With a `month`, the facts
    carry `month_fraction` and `month_verdict`; with none, those keys are absent and the facts are as before."""
    fraction = weekly_fraction(weekly)
    month_fraction = month.fraction if month is not None else None
    month_stopped = month_fraction is not None and month_fraction >= policy.weekly_hard_stop_fraction
    pace = pacing.weekly_pace(weekly, policy, now) if now is not None else None
    lane_fraction = pace.lane_fraction if pace is not None else 1.0
    return cast(LimitsFacts, {
        "hard_stop": fraction >= policy.weekly_hard_stop_fraction or month_stopped,
        "weekly_fraction": fraction,
        "hard_stop_fraction": policy.weekly_hard_stop_fraction,
        "launch_cap": scaled_cap(max_in_flight, lane_fraction) if assessment.verdict in LAUNCHING_VERDICTS else 0,
        "lane_fraction": lane_fraction,
        "weekly_target_fraction": pace.target_fraction if pace is not None else None,
        "weekly_pace": pace.verdict if pace is not None else "not_judged",
        "go_degraded": assessment.verdict == "go_degraded",
        "five_hour_fraction": assessment.spent_fraction,
        "window_start_day": window_start_day(weekly.start) if weekly is not None else None,
        "window_source": window_source,
        "weekly_source": weekly_source,
        "smoke_hold": chair_smoke.read_hold(runs_dir) if runs_dir else None,
        **({} if month is None else {"month_fraction": month_fraction, "month_verdict": month.verdict}),
    })


Key = tuple[str, str, str]


def run_initiative(run: str) -> str:
    """The initiative a run id belongs to: `x-3` is `x`, as the `runs:<initiative>` store lease is named."""
    return re.sub(r"-\d+$", "", run)


def _key(row: Row) -> Key:
    """(initiative, phase, task). A task id repeats across phases, so phase is part of the key.

    A row with no initiative, such as every `runs_stranded` row, takes it from its run id."""
    initiative = str(row.get("initiative") or run_initiative(str(row.get("run") or "")))
    return initiative, str(row.get("phase") or ""), str(row.get("task") or "")


def harness_failures(attempts: Sequence[Row], key: Key) -> int:
    """The task's harness-cause attempts over all runs, on the task's current body. A retry that fails again reads
    one higher. A row with no `on_current_body` counts, as it does today."""
    return sum(
        1
        for a in attempts
        if _key(a) == key and a.get("cause") == HARNESS_CAUSE and a.get("on_current_body", True)
    )


def rescue_failed(attempts: Sequence[Row], key: Key) -> bool:
    """Whether the task has a rescue_failed attempt, over the same attempts and key `harness_failures` counts."""
    return any(_key(a) == key and a.get("cause") == RESCUE_FAILED_CAUSE for a in attempts)


def newest_cause(attempts: Sequence[Row], key: Key) -> str:
    """The cause on the task's newest attempt, from the list `harness_failures` counts. Empty with no attempt."""
    return next((str(a.get("cause") or "") for a in reversed(attempts) if _key(a) == key), "")


def _on_body(attempts: Sequence[Row], key: Key) -> list[Row]:
    """The task's attempts on its current body, oldest first. A row with no `on_current_body` counts."""
    return [a for a in attempts if _key(a) == key and a.get("on_current_body", True)]


# A stoppage is the harness's, not the task's: a limit, a login, the infrastructure, a stale base, an interrupted run.
# A failed rescue checks work already built. Neither counts toward `chair.quarantine_hold_after`, whatever cause the
# attempt carries: a rescue_failed attempt may name code or review. The kinds are the ones the epic's cap skips
# (`harness.epic._build_attempts`).
_NOT_A_FAILURE = frozenset({HARNESS_CAUSE, RESCUE_FAILED_CAUSE})
_NOT_A_BUILD = frozenset({"rescue_failed", "infra", "interrupted"})


def task_failures(attempts: Sequence[Row], key: Key) -> int:
    """The task's own failures over every body it had: each build attempt whose cause is not a stoppage or a rescue."""
    return sum(
        1
        for a in attempts
        if _key(a) == key and str(a.get("cause") or "") not in _NOT_A_FAILURE and str(a.get("kind") or "") not in _NOT_A_BUILD
    )


def _quarantine(
    key: Key,
    cause: str,
    attempts: Sequence[Row],
    has_patch: Callable[[str, str], bool],
    row: Row | None = None,
) -> QuarantineFacts:
    """One quarantine. `row` is the open quarantine's own reader row: its newest run and reason, which the sort acts on.

    A review or code quarantine holds its reason as `held_objection`; a harness quarantine names its state by its
    newest run, so it is rebuilt once per run. `failures` counts the task's own failures over every body, which
    `chair.quarantine_hold_after` holds the task at."""
    on_body = _on_body(attempts, key)
    fact: QuarantineFacts = {
        "task_id": key[2],
        "initiative": key[0],
        "cause": cause,
        "harness_failures": harness_failures(attempts, key),
        "has_patch": has_patch(key[0], key[2]),
        "rescue_failed": rescue_failed(attempts, key),
    }
    if row is None:
        return fact
    reason, run = str(row.get("reason") or ""), str(row.get("run") or "")
    fact = {
        **fact,
        "causes": [str(a.get("cause") or "") for a in on_body],
        "reasons": [str(a.get("reason") or "") for a in on_body],
        "run": run,
        "reason": reason,
        "failures": task_failures(attempts, key),
    }
    if cause in ("review", "code") and reason.strip():
        fact["held_objection"] = reason
    if cause == HARNESS_CAUSE and run:
        fact["task_state_key"] = run
    return fact


def with_attempt_runs(quarantined: Sequence[Row], attempts: Sequence[Row]) -> list[Row]:
    """Each quarantine row that names no run takes `run` and `reason` from its task's newest attempt on the current body.

    Under work_state store a queue row's `extra` holds no attempts, so `quarantined_from_rows` finds no run; the attempts
    reader (`chair_read_run_records`) finds it in the run task records. A row that names a run keeps it, and a task with
    no attempt on its current body keeps its empty run, so it still reaches the chair."""
    def newest(q: Row) -> Row | None:
        return next(reversed(_on_body(attempts, _key(q))), None)

    return [
        {**q, "run": str(a.get("run") or ""), "reason": str(a.get("reason") or "")}
        if not q.get("run") and (a := newest(q)) is not None else q
        for q in quarantined
    ]


def awaiting_fetch(quarantined: Sequence[Row], unfetched: Mapping[str, str]) -> list[AwaitingFetch]:
    """The quarantines that wait for a remote run's fetch: their initiative's unfetched epic run (`remote_unfetched`) is
    later than the run that names the quarantine, so its task records, still on the lane host, may hold a newer attempt.

    One initiative's runs never overlap (its `runs:<initiative>` lease), so a later run number is a later run. A
    quarantine that names no run waits for any: the unfetched run may be the one that quarantined it. An older unfetched
    run never holds back a later quarantine. A decompose-only unfetched run writes no task record, so nothing waits on it.
    Each entry names the run it waits for; the status line shows it, so a waiting quarantine is never hidden."""
    decompose = frozenset(getattr(unfetched, "decompose", ()))
    return [
        {"initiative": str(q["initiative"]), "task_id": str(q["task"]), "run": unfetched[q["initiative"]]}
        for q in quarantined
        if q["initiative"] in unfetched and q["initiative"] not in decompose
        and _run_number(unfetched[q["initiative"]]) > _run_number(str(q.get("run") or ""))
    ]


def quarantine_facts(
    quarantined: Sequence[Row],
    stranded: Sequence[Row],
    attempts: Sequence[Row],
    live: Collection[str],
    has_patch: Callable[[str, str], bool],
    landing: Sequence[ApprovedTask] = (),
    approved: Sequence[Row] | None = None,
    waiting: Collection[tuple[str, str]] = frozenset(),
) -> list[QuarantineFacts]:
    """One entry per open quarantine with its newest cause, then the stranded rows those did not name.

    `landing` is what `chair_plan_land.planned_tasks` plans this tick. A stranded row for one of those tasks
    that carries a run is left out, since its land handles it; a land that then fails raises its own needs_chair.

    `approved` is the tasks the store has approved, keys id, initiative. A stranded row whose task is not among them
    is stale, since the store sent it back, and is left out. None keeps every stranded row.

    A stranded row with `expected` true is left out. The remaining stranded rows of one run give one entry, whose
    `tasks` lists each task with its run and remedy. Its `task_id` is the first task not in `waiting`, else the first.

    `waiting` is the (initiative, task id) pairs of approved, unlanded rows. `chair_plan_recover._recovery` plans nothing
    for a stranded entry whose `task_id` is one of them, so a run's lead task is one that is not, and one such task keeps
    the run's note alive.

    An open quarantine whose attempts name no cause (a store row in state quarantined with no run task record that
    names one) reports `QUARANTINED_CAUSE`, so the status line never shows it with an empty cause.

    An initiative with a live run contributes nothing: a retry in flight is never planned again."""
    opened = list(dict.fromkeys(_key(q) for q in quarantined))
    by_key = {_key(q): q for q in quarantined}
    landed_now = {(t["initiative"], t["phase"], t["id"]) for t in landing if t["run"]}
    # A stranded task that is also an open quarantine keeps the quarantine's newest cause, so it is not sent to the chair as stranded.
    # Matched on initiative and task id, as `chair_plan_recover._recovery` does: a stranded record's phase can be missing or old.
    in_store = None if approved is None else {(t["initiative"], t["id"]) for t in approved}
    # An `expected` row is a deliberate manual landing: it is reported by the reader but raises no note.
    kept = [s for s in stranded if not s.get("expected")]
    rows = {k: next(s for s in kept if _key(s) == k) for k in dict.fromkeys(_key(s) for s in kept)}
    stuck = [
        k for k in rows
        if k not in opened and k not in landed_now and (in_store is None or (k[0], k[2]) in in_store)
    ]
    return [
        *(_opened_quarantine(k, attempts, has_patch, by_key[k]) for k in opened if k[0] not in live),
        *(_stranded_entry(ks, rows, attempts, has_patch, waiting) for ks in _by_run(stuck, rows) if ks[0][0] not in live),
    ]


def _opened_quarantine(key: Key, attempts: Sequence[Row], has_patch: Callable[[str, str], bool], row: Row) -> QuarantineFacts:
    """One open quarantine. Its cause is the chair's own mark on the row while no attempt from a later run outdates it
    (`chair_phase_quarantine.marked_cause`), with the mark's reason; else the newest attempt's, else `QUARANTINED_CAUSE`."""
    mark = marked_cause(row.get("mark"), [a for a in attempts if _key(a) == key])
    if mark is None:
        return _quarantine(key, newest_cause(attempts, key) or QUARANTINED_CAUSE, attempts, has_patch, row)
    return _quarantine(key, mark["cause"], attempts, has_patch, {**row, "reason": mark["reason"] or row.get("reason") or ""})


def _by_run(keys: Sequence[Key], rows: Mapping[Key, Row]) -> list[list[Key]]:
    """`keys` grouped by (initiative, run), in first-seen order. A row with no run groups under the empty run."""
    def group(k: Key) -> tuple[str, str]:
        return k[0], str(rows[k].get("run") or "")

    return [[k for k in keys if group(k) == g] for g in dict.fromkeys(group(k) for k in keys)]


def _stranded_entry(
    keys: Sequence[Key],
    rows: Mapping[Key, Row],
    attempts: Sequence[Row],
    has_patch: Callable[[str, str], bool],
    waiting: Collection[tuple[str, str]] = frozenset(),
) -> QuarantineFacts:
    """One stranded entry for one run's tasks. `task_id` is the first task not in `waiting`, else the first; `tasks` lists every one in row order.

    The run rides inside `tasks`, not as a top-level `run`: `launch_history_from_actions` reads that key as a quarantined run."""
    lead = next((k for k in keys if (k[0], k[2]) not in waiting), keys[0])
    return cast(QuarantineFacts, {
        **_quarantine(lead, STRANDED_CAUSE, attempts, has_patch),
        "tasks": [{"task": k[2], "run": rows[k].get("run"), "remedy": rows[k].get("remedy")} for k in keys],
    })


def store_approved_tasks(rows: Sequence[Row]) -> list[Row]:
    """The tasks whose store row says approved, keys id, initiative: the `approved` `quarantine_facts` filters by.

    Taken from the same `rows` `stranded_from_rows` reads each stranded row's state from, so the two agree by
    construction. `gather_facts`'s own `approved` list is a separate read that also closes landed tasks, so it can
    differ within a tick and is not used here.

    Known gap: `runs_stranded` flags a ready task as stranded so its orphaned run branch is reported. A ready task
    is never in this list, so in store mode that report is dropped too, whether or not the task was rebuilt."""
    return [
        {"id": str(r.get("task_id") or ""), "initiative": str(r.get("initiative") or "")}
        for r in rows if r.get("kind") == "task" and r.get("state") == "approved"
    ]


def _surfaces_of(item: Row) -> list[str]:
    raw = item.get("surfaces")
    return [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []


def ticket_surfaces(items: Sequence[Row], ready_ids: Mapping[str, Collection[str]]) -> dict[str, list[str]]:
    """Initiative to the sorted union of the surfaces of its ready tasks; a task with no surfaces adds nothing."""
    return {
        initiative: sorted({s for i in items if i["initiative"] == initiative and i["id"] in ids for s in _surfaces_of(i)})
        for initiative, ids in ready_ids.items()
    }


def phase_facts(rows: Sequence[Row], land_at: str, claimed: Collection[str] = frozenset()) -> list[PhaseFacts]:
    """The phases of one initiative that can start a lane under `land_at`, each with its ready tasks and their surfaces.

    A task id in `claimed` (a live lease holds it) is not ready, so a phase left with none is not listed.
    `requires` is read as the docket reads it: a scalar `requires: gpu` is `["gpu"]`."""
    by_id = {str(r["id"]): r for r in rows}
    free = {phase: [i for i in ids if i not in claimed] for phase, ids in launchable_phases(rows, land_at).items()}
    return [
        {
            "phase": phase,
            "ready_tasks": [
                {
                    "id": i,
                    "phase": phase,
                    "needs": [str(n) for n in by_id[i].get("needs") or []],
                    "requires": queue_rows.requires_of(by_id[i]),
                }
                for i in ids
            ],
            "surfaces": sorted({s for i in ids for s in _surfaces_of(by_id[i])}),
        }
        for phase, ids in free.items()
        if ids
    ]


def with_landing_merged(items: Sequence[Row], records: Sequence[Mapping[str, Any]], land_at: Mapping[str, str]) -> list[Row]:
    """Pure. `items` with the merged flag of each approved task set from the landing record, for initiatives at `initiative`.

    `merged` has one meaning there: the task's phase is merged into the initiative branch. A merged task holds no
    files and its phase's children may start. An initiative with no record yet has nothing merged. Items of an
    initiative that lands at `task`, and items in any other state, are returned as they came."""
    from core import landing_record as lr  # lazy: core sits beside agent_tools in one venv
    from core.landing import is_initiative

    by_initiative = {str(r.get("initiative")): r for r in records}
    own: dict[str, list[Row]] = {}
    for item in items:
        own.setdefault(str(item["initiative"]), []).append(item)
    merged = {
        initiative: set(lr.merged_tasks(cast("lr.LandingRecord | None", by_initiative.get(initiative)), [
            {"id": str(r["id"]), "phase": str(r.get("phase") or ""), "state": r.get("state"), "needs": list(r.get("needs") or [])}
            for r in rows
        ]))
        for initiative, rows in own.items()
        if is_initiative(land_at.get(initiative, "initiative"))
    }
    return [
        {**item, "merged": str(item["id"]) in merged[initiative]}
        if (initiative := str(item["initiative"])) in merged and item.get("state") == "approved"
        else item
        for item in items
    ]


PHASE_RUN_STATES = frozenset({"in_progress", "ready"})  # what a live phase run holds files for


def running_initiatives(
    live: Collection[str],
    items: Sequence[Row],
    repos: Mapping[str, str],
    *,
    unlanded: Collection[str] = frozenset(),
    live_phases: Collection[tuple[str, str]] = (),
) -> list[RunningInitiative]:
    """Live initiatives hold ready and approved surfaces; a non-live unlanded one holds approved surfaces only.

    An approved item whose merged flag is true holds nothing, live or not. An initiative with only ready tasks and no
    live run has no entry. `live` trusts an unexpired `runs:` lease or a live pid, so a finished run holds files until its lease expires.
    Each `live_phases` pair adds an entry with `phase` set, holding the surfaces of that phase's in-progress and ready items only.
    `state` is `running` for a live initiative or phase and `awaiting_land` for a non-live entry held by approved unmerged items."""

    def held(initiative: str) -> list[Row]:
        states = STEER_STATES if initiative in live else frozenset({"approved"})
        return [
            i for i in items
            if i["initiative"] == initiative and i.get("state") in states
            and not (i.get("state") == "approved" and i.get("merged"))
        ]

    return cast(list[RunningInitiative], [
        {
            "id": initiative,
            "repo": repos.get(initiative, ""),
            "surfaces": sorted({s for i in held(initiative) for s in _surfaces_of(i)}),
            "live": initiative in live,
            "state": "running" if initiative in live else "awaiting_land",
        }
        for initiative in sorted(set(live) | set(unlanded))
        if initiative in live or held(initiative)
    ] + [
        {
            "id": initiative,
            "repo": repos.get(initiative, ""),
            "surfaces": sorted(
                {
                    s for i in items
                    if i["initiative"] == initiative and i.get("phase") == phase and i.get("state") in PHASE_RUN_STATES
                    for s in _surfaces_of(i)
                }
            ),
            "phase": phase,
            "live": True,
            "state": "running",
        }
        for initiative, phase in sorted(set(live_phases))
    ])


SCHEDULE_TERMINAL = frozenset({"done", "dropped"})
SCHEDULE_WAITING = frozenset({"approved", "quarantined", "waiting", "in_progress"})  # held, but by no live run unless one is named


def phase_status(states: Collection[str], launchable: bool, held: bool) -> str:
    """`running` only when a live run `held` the phase. Otherwise `complete` when every task is terminal, `waiting` when a
    task is approved, quarantined, waiting or in progress with no live run, `ready` when launchable, else `blocked`.
    A `todo` task is the normal state of a later phase and never makes it running."""
    if held:
        return "running"
    if set(states) <= SCHEDULE_TERMINAL:
        return "complete"
    if set(states) & SCHEDULE_WAITING:
        return "waiting"
    return "ready" if launchable else "blocked"


def _phase_size(rows: Sequence[Row]) -> str:
    return next((str(r["size"]) for r in rows if r.get("size")), "")


def _phase_mix(rows: Sequence[Row]) -> str:
    return "+".join(sorted({c for r in rows for c in queue_rows.requires_of(r)})) or "plain"


def phase_parents(rows: Sequence[Row], *, open_only: bool) -> dict[str, set[str]]:
    """Phase to the other phases a task of it needs; with `open_only`, a need already `done` adds none."""
    phase_of = {str(r["id"]): str(r.get("phase") or "") for r in rows}
    state_of = {str(r["id"]): r.get("state") for r in rows}
    edges = {
        (str(r.get("phase") or ""), phase_of[str(n)])
        for r in rows
        for n in r.get("needs") or []
        if str(n) in phase_of and not (open_only and state_of[str(n)] == "done")
    }
    return {p: {q for here, q in edges if here == p and q and q != p} for p in set(phase_of.values()) if p}


def schedule_phases(
    initiative: str, rows: Sequence[Row], land_at: str, live_phases: Collection[tuple[str, str]]
) -> list[SchedulePhase]:
    """Every phase of one initiative that is not complete, in first-appearance order, with its parents and surfaces.

    Parents are the phases a task of this phase still needs. A phase is `running` only when its (initiative, phase)
    pair is in `live_phases`; a task in progress with no such pair reads `waiting`. Surfaces are those
    of the phase's open tasks, ready or not; `ticket_surfaces` keeps ready ones only."""
    parents = phase_parents(rows, open_only=True)
    launchable = launchable_phases(rows, land_at)

    def one(phase: str) -> SchedulePhase:
        own = [r for r in rows if str(r.get("phase") or "") == phase]
        states = [str(r.get("state")) for r in own]
        return {
            "id": phase,
            "status": phase_status(states, phase in launchable, (initiative, phase) in live_phases),  # type: ignore[typeddict-item]
            "parents": sorted(parents.get(phase, ())),
            "size": _phase_size(own),
            "mix": _phase_mix(own),
            "surfaces": sorted({s for r in own if r.get("state") not in SCHEDULE_TERMINAL for s in _surfaces_of(r)}),
        }

    phases = [one(p) for p in dict.fromkeys(str(r["phase"]) for r in rows if r.get("phase"))]
    return [p for p in phases if p["status"] != "complete"]


def duration_records(
    actions: Sequence[Row],
    shapes: Mapping[tuple[str, str], tuple[str, str]],
    parents: Mapping[tuple[str, str], Collection[str]],
) -> list[dict[str, str]]:
    """Estimator records, one per phase the chair landed, from the `chair_actions` rows the tick already reads.

    A `land_phase` row with status `landed` carries initiative, phase, run and `ts`; its `ts` is the phase's end.
    The start is the latest `ts` of a landed parent phase of the same run, else the newest done `launch_epic` or
    `relaunch` row of the initiative at or before the end. The end is when the land finished, so it runs a little
    past the build. A phase landed per ticket writes no `land_phase` row and gives no record, and neither does a
    phase with no start or no entry in `shapes`, which maps (initiative, phase) to (size, mix)."""
    docs = sorted(map(_action_doc, actions), key=lambda d: str(d.get("ts") or ""))
    launches = [
        (str(d.get("initiative") or d.get("target")), str(d["ts"]))
        for d in docs
        if d.get("kind") in ("launch_epic", "relaunch") and d.get("status") == "done"
        and (d.get("initiative") or d.get("target")) and d.get("ts")
    ]
    lands = {
        (str(d["initiative"]), str(d["phase"]), str(d.get("run") or "")): str(d["ts"])
        for d in reversed(docs)
        if d.get("kind") == "land_phase" and d.get("status") == "landed" and d.get("initiative") and d.get("phase") and d.get("ts")
    }

    def one(key: tuple[str, str, str], end: str) -> dict[str, str] | None:
        initiative, phase, run = key
        after = [t for p in parents.get((initiative, phase), ()) if (t := lands.get((initiative, p, run))) and t < end]
        before = [t for i, t in launches if i == initiative and t <= end]
        start = max(after) if after else (before[-1] if before else None)
        shape = shapes.get((initiative, phase))
        if start is None or shape is None:
            return None
        return {SIZE_KEY: shape[0], MIX_KEY: shape[1], START_KEY: start, END_KEY: end}

    return [r for key, end in sorted(lands.items(), key=lambda kv: kv[1]) if (r := one(key, end)) is not None]


def schedule_hosts(dispatch: DispatchFacts, local: str) -> tuple[list[ScheduleHost], dict[str, int], str]:
    """Hosts with one lane per slot, live load by host, and the orchestration mode, from the dispatch facts.

    The local machine has `max_in_flight` lanes; a remote host has its `capacity`, or one lane when it names none.
    `local_lanes` "decompose" is orchestration, as `chair_orchestrate` reads it."""
    slots = [(local, int(dispatch["max_in_flight"]), True, int(dispatch["live_runs"]))] + [
        (h["name"], int(h.get("capacity", 1)), False, int(h["live_runs"])) for h in dispatch["hosts"]
    ]
    hosts: list[ScheduleHost] = [
        {"name": name, "lanes": [f"{name}-{n}" for n in range(1, count + 1)], "local": is_local, "repos": []}
        for name, count, is_local, _ in slots
    ]
    mode = "orchestrating" if dispatch.get("local_lanes") == "decompose" else "building"
    return hosts, {name: live for name, _, _, live in slots}, mode


def running_lanes(
    hosts: Sequence[ScheduleHost],
    live_phases: Collection[tuple[str, str]],
    newest_run_host: Mapping[str, str] | None,
    local: str,
) -> list[ScheduleRunning]:
    """Each live phase with its newest run's host and the next unused lane of that host, in `hosts` lane order.

    A phase on an unknown host, or past that host's lane count, is left out. Phases are taken in sorted order."""
    names = {h["name"] for h in hosts}
    wanted = [
        (i, p, (newest_run_host or {}).get(i) or local) for i, p in sorted(live_phases)
    ]
    kept = [(i, p, h) for i, p, h in wanted if h in names]
    lanes = {h["name"]: h["lanes"] for h in hosts}
    ordinal = {
        (i, p): sum(1 for _, _, other in kept[:n] if other == h) for n, (i, p, h) in enumerate(kept)
    }
    return [
        {"initiative": i, "phase": p, "host": h, "lane": lanes[h][ordinal[(i, p)]]}
        for i, p, h in kept
        if ordinal[(i, p)] < len(lanes[h])
    ]


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def release_target(work: Sequence[Row], open_ids: Collection[str]) -> str | None:
    """The earliest `release_target` an open initiative's work row states, else None."""
    found = [
        str(t)
        for r in work
        if r.get("kind") == "initiative" and r.get("initiative") in open_ids
        if (t := r.get("release_target") or (r.get("extra") or {}).get("release_target"))
    ]
    return min(found) if found else None


def schedule_inputs_from_rows(
    items: Sequence[Row],
    work: Sequence[Row],
    actions: Sequence[Row],
    dispatch: DispatchFacts,
    *,
    local: str,
    repos: Mapping[str, str],
    land_at: Mapping[str, str],
    live_phases: Collection[tuple[str, str]],
    newest_run_host: Mapping[str, str] | None = None,
) -> ScheduleInputs:
    """The scheduler's inputs, from rows already read; no read, no clock.

    `newest_run_host` maps an initiative to its newest run's host, "" meaning local; None means every live phase is local.

    `items` are the ticket rows, `work` the work rows (priority, release target), `actions` the chair action rows
    for `duration_records`. `repos`, `land_at` and `live_phases` have no default: an edge that forgets one would
    give every initiative an empty repo and the default land mode without a sign. An initiative with no open phase is left out."""
    ids = list(dict.fromkeys(str(r["initiative"]) for r in items))
    own = {i: [r for r in items if str(r["initiative"]) == i] for i in ids}
    priority = {
        str(r["initiative"]): _int(r.get("priority", (r.get("extra") or {}).get("priority")))
        for r in work
        if r.get("kind") == "initiative"
    }
    initiatives: list[ScheduleInitiative] = [
        {"id": i, "priority": priority.get(i, 0), "repo": repos.get(i, ""), "phases": phases}
        for i in ids
        if (phases := schedule_phases(i, own[i], land_at.get(i, "initiative"), live_phases))
    ]
    shapes = {
        (i, p): (_phase_size(rs), _phase_mix(rs))
        for i in ids
        for p in {str(r["phase"]) for r in own[i] if r.get("phase")}
        if (rs := [r for r in own[i] if str(r.get("phase") or "") == p])
    }
    edges = {(i, p): q for i in ids for p, q in phase_parents(own[i], open_only=False).items()}
    hosts, load, mode = schedule_hosts(dispatch, local)
    return {
        "initiatives": initiatives,
        "records": duration_records(actions, shapes, edges),
        "hosts": hosts,
        "mode": mode,
        "load": load,
        "target": release_target(work, {i["id"] for i in initiatives}),
        "running": running_lanes(hosts, live_phases, newest_run_host, local),
    }


def _action_doc(row: Row) -> Row:
    raw = row.get("action_json")
    doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return {**row, **(doc if isinstance(doc, dict) else {})}


def _run_number(run: str) -> int:
    match = re.search(r"-(\d+)$", run)
    return int(match.group(1)) if match else -1
