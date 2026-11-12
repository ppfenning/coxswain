"""The edge of a chair tick: gather `chair_types.Facts` from injected sources.

The shapers are pure and `gather_facts` calls each source once. The only clock is the `now` argument.
No retry state is stored: `harness_failures` is counted from run history on every call.
"""
import json
import math
import re
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial, reduce
from itertools import takewhile
from pathlib import Path
from typing import Any, cast

from agent_tools import chair_lease_refusal, chair_read_docket, chair_smoke, forge, pacing, queue_rows, repo_path, route
from agent_tools.budget import MonthBudget
from agent_tools.chair import lease_holder
from agent_tools.chair_decompose_streak import DecomposeRun, DecomposeStreak, at_limit, streak
from agent_tools.chair_idle_stall import IDLE_STALL_MINUTES
from agent_tools.chair_land_backoff import LandRefusal, aligned
from agent_tools.chair_land_freeze import LandFreeze, read_freeze
from agent_tools.chair_launch_budget import DEFAULT_MAX_LAUNCHES_PER_HOUR, WINDOW
from agent_tools.chair_phases import launchable_phases
from agent_tools.chair_plan import initiative_homes
from agent_tools.chair_plan_land import planned_tasks
from agent_tools.chair_read_dag import dag_problems
from agent_tools.chair_read_docket import docket_from_rows
from agent_tools.chair_read_intake import intake_from_rows
from agent_tools.chair_read_land_conflicts import _rev_parse, read_land_conflicts
from agent_tools.chair_read_land_heads import read_land_heads
from agent_tools.chair_read_quarantined import quarantined_from_rows
from agent_tools.chair_read_steward import reported_keys, task_signature
from agent_tools.chair_read_stranded import stranded_from_rows
from agent_tools.chair_read_tuning import current_lanes, tuning_facts
from agent_tools.chair_schedule_durations import END_KEY, MIX_KEY, SIZE_KEY, START_KEY
from agent_tools.chair_steward import StewardFacts, StewardTask
from agent_tools.chair_types import (
    EASTERN,
    HELD_FILE_CAUSE,
    QUARANTINED_CAUSE,
    STALE_CHECKOUT_CAUSE,
    ApprovalHold,
    ApprovedTask,
    CarryTask,
    DispatchFacts,
    EmptyDecomposeFacts,
    EpicFacts,
    Facts,
    HeldFile,
    HoldRecord,
    HostCheck,
    IdleStallInputs,
    InitiativeFacts,
    LandedMain,
    LandFailStreak,
    LandWait,
    LandWatch,
    LaunchHistory,
    LeaseFacts,
    LeaseRefusalStreak,
    LimitsFacts,
    LivePhase,
    MergeQueueFacts,
    PhaseBranch,
    PhaseFacts,
    PhaseTasks,
    PidProbeFact,
    QuarantineFacts,
    QueueFile,
    RemoteUnfetched,
    ReviewPr,
    RunningInitiative,
    RunRecord,
    ScheduleHost,
    ScheduleInitiative,
    ScheduleInputs,
    SchedulePhase,
    ScheduleRunning,
    StewardPlanFacts,
    StrandedPhase,
    TodoInitiative,
    WronglyReleased,
)
from agent_tools.checkout_env import CHECKOUT_ENV_KEY
from agent_tools.host_load import resolve_load_ceiling
from agent_tools.land_batch_settings import merge_queue_setting
from agent_tools.release_gate import deferred_initiatives, held_initiatives
from agent_tools.remote_lane import fetched_record_path, remote_record_path

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
        path adds phase and task. The store's `attempts` table has no task column (`run_store.attempt_causes`).
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
    shared_file_limit: the raw profile value at `chair.max_shared_file_initiatives`, resolved by
        `resolve_shared_file_limit`. Optional, and absent means 2.
    serial_files: the raw profile value at the top-level `serial_files`, resolved by `resolve_serial_files`.
        Optional, and absent means no serial paths.
    host_load_ceiling: the raw profile value at `chair.host_load_ceiling`, resolved by `host_load.resolve_load_ceiling`.
        Optional, and absent means no ceiling.
    merge_queue: the raw profile value at the top-level `merge_queue`, resolved by `merge_queue_fact`. Optional, and
        absent means the queue is off.
    approval_hold_ticks: the raw profile value for the approval hold limit, resolved by `resolve_approval_hold_ticks`.
        Optional, and absent means 6 ticks.
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
        `launch_history` and `relaunch_loop_reported` derive from them.
        Build it with `windowed_actions` so one gather reads the table once and every caller shares the rows.
        Optional, and absent means {}.
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
    steward_enabled: the raw profile value that turns the epic steward on, resolved by `resolve_steward_enabled`.
        Defaults to a callable returning None, meaning off: `gather_facts` adds no `steward` key and calls no other steward source.
    steward_scope: the raw profile value of chair `steward_scope`, resolved by `resolve_steward_scope`: "all" plans every
        open initiative, "epics" only unlanded epic members. Defaults to a callable returning None, meaning "all".
    steward_entries: every bus entry (`chair_read_steward.read_bus_entries`); `reported_keys` turns the typed ones
        into the steward's reported set. Defaults to a callable returning [].
    ticket_head: initiative to its ticket branch head commit. Defaults to a callable returning "".
    level_merged: (initiative, phase) to whether that level branch has landed on the ticket branch, bound in
        production to `chair_read_steward.level_is_merged`. Defaults to a callable returning False.
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
    approval_hold_ticks: Callable[[], object] | None = None  # raw approval hold limit setting; absent means 6 ticks
    max_launches_per_hour: Callable[[], object] | None = None  # raw profile chair.max_launches_per_hour; absent means the default
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
    steward_enabled: Callable[[], object] = lambda: None  # raw profile value that turns the steward on; absent means off
    steward_scope: Callable[[], object] = lambda: None  # raw profile value of steward_scope; absent means "all"
    steward_entries: Callable[[], Sequence[Row]] = lambda: []  # every bus entry; absent means []
    ticket_head: Callable[[str], str] = lambda initiative: ""  # initiative to its ticket branch head; absent means ""
    level_merged: Callable[[str, str], bool] = lambda initiative, phase: False  # (initiative, phase) to whether the level landed
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
    return {
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
    }


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
    return {
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
    }


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


def _quarantine(
    key: Key, cause: str, attempts: Sequence[Row], has_patch: Callable[[str, str], bool]
) -> QuarantineFacts:
    return {
        "task_id": key[2],
        "initiative": key[0],
        "cause": cause,
        "harness_failures": harness_failures(attempts, key),
        "has_patch": has_patch(key[0], key[2]),
        "rescue_failed": rescue_failed(attempts, key),
    }


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

    An open quarantine whose attempts name no cause (a store row in state quarantined whose ticket file never got the
    attempt) reports `QUARANTINED_CAUSE`, so the status line never shows it with an empty cause.

    An initiative with a live run contributes nothing: a retry in flight is never planned again."""
    opened = list(dict.fromkeys(_key(q) for q in quarantined))
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
        *(_quarantine(k, newest_cause(attempts, k) or QUARANTINED_CAUSE, attempts, has_patch) for k in opened if k[0] not in live),
        *(_stranded_entry(ks, rows, attempts, has_patch, waiting) for ks in _by_run(stuck, rows) if ks[0][0] not in live),
    ]


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
        initiative: set(lr.merged_tasks(by_initiative.get(initiative), [
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

    return [
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
    ]


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
    return reduce(_streak_step, map(_action_doc, ordered), {})


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
    docs = list(map(_action_doc, sorted(actions, key=lambda r: str(r.get("ts") or ""))))
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
        return {**reported, initiative: f"{doc.get('phase')}|{doc.get('quarantined')}|{doc.get('awaiting_land')}"}
    if doc.get("kind") in LAUNCH_KINDS and initiative:
        return {k: v for k, v in reported.items() if k != initiative}
    return dict(reported)


def waiting_reported_from_actions(actions: Sequence[Row]) -> dict[str, str]:
    """Initiative to `<phase>|<quarantined>|<awaiting_land>` of its newest waiting row, until a later launch of it."""
    ordered = sorted(actions, key=lambda r: str(r.get("ts") or ""))
    return reduce(_waiting_step, map(_action_doc, ordered), {})


def pull_age_s(actions: Sequence[Row], now: datetime) -> float | None:
    """Seconds from the newest `pull` row's `ts` to `now`; None when no row is a `pull` with a parseable `ts`."""
    stamps = [
        at
        for doc in map(_action_doc, actions)
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
    docs = [_action_doc(row) for row in actions]
    return [
        f"{d['initiative']}|{d['run']}"
        for d in docs
        if d.get("kind") == "needs_chair" and d.get("cause") == "decompose_stalled" and d.get("initiative") and d.get("run")
    ]


def held_file_keys(actions: Sequence[Row]) -> list[str]:
    """`<path>|<blob>` of each held_file needs_chair row, in row order."""
    docs = [_action_doc(row) for row in actions]
    return [
        f"{d['path']}|{d.get('blob', '')}"
        for d in docs
        if d.get("kind") == "needs_chair" and d.get("cause") == HELD_FILE_CAUSE and d.get("path")
    ]


def stale_checkout_keys(actions: Sequence[Row]) -> list[str]:
    """`<host>|<repo>` of each stale_checkout needs_chair row, in row order."""
    docs = [_action_doc(row) for row in actions]
    return [
        f"{d['host']}|{d['repo']}"
        for d in docs
        if d.get("kind") == "needs_chair"
        and d.get("cause") == STALE_CHECKOUT_CAUSE
        and d.get("host")
        and d.get("repo")
    ]


def _checkout_env_of(versions_json: object) -> dict:
    raw = versions_json if isinstance(versions_json, (str, bytes)) else None
    try:
        versions = json.loads(raw) if raw is not None else versions_json
    except ValueError:
        return {}
    entries = versions.get(CHECKOUT_ENV_KEY) if isinstance(versions, dict) else None
    return dict(entries) if isinstance(entries, dict) else {}


def host_env_from_rows(hosts: Sequence[Row]) -> dict[str, dict]:
    """Host name to the checkout_env record in its versions_json; {} when the key is absent or the JSON unreadable."""
    return {h["name"]: _checkout_env_of(h.get("versions_json")) for h in hosts if h.get("name")}


def repo_dir_name(repo: str) -> str:
    """The last path component of an initiative's `repo:`, the key of the beat's checkout_env record; "" when empty."""
    return repo.rstrip("/").rpartition("/")[2]


_LOOP_RUNS = re.compile(r"runs (\S+) and (\S+) were both quarantined")


def relaunch_loop_keys(actions: Sequence[Row]) -> list[str]:
    """`<run1>|<run2>` of each relaunch_loop needs_chair row, in row order; the row names both runs only in its reason."""
    docs = [_action_doc(row) for row in actions]
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
    docs = sorted((_action_doc(row) for row in actions), key=lambda d: str(d.get("ts") or ""))
    lands = [d for d in docs if d.get("kind") in ("land_phase", "partial_land") and d.get("initiative") and d.get("phase") and d.get("run")]
    keys = dict.fromkeys((d["initiative"], d["phase"], d["run"]) for d in lands)

    def refusal(key: tuple[str, str, str]) -> LandRefusal | None:
        rows = [d for d in lands if (d["initiative"], d["phase"], d["run"]) == key]
        last_landed = max((n for n, d in enumerate(rows) if d.get("status") == "landed"), default=-1)
        failed = [
            d
            for d in rows[last_landed + 1 :]
            if d.get("status") in LAND_UNLANDED_STATUSES and (now is None or _within_horizon(d.get("ts"), now, horizon))
        ]
        if not failed:
            return None
        head = failed[-1].get("head")
        return {
            "initiative": key[0], "phase": key[1], "run": key[2],
            "attempts": len(failed), "last_refused_at": str(failed[-1]["ts"]),
            **({"head": head} if isinstance(head, str) and head else {}),
        }

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
    docs = map(_action_doc, sorted(actions, key=lambda r: str(r.get("ts") or "")))
    rows = [d for d in docs if d.get("kind") in LAND_FAIL_KINDS and d.get("initiative") and d.get("phase")]
    state = reduce(_fail_step, rows, {})
    return [
        {"initiative": i, "phase": p, "kind": cast("Any", kind), "reason": reason, "ticks": ticks}
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
    docs = list(map(_action_doc, sorted(actions, key=lambda r: str(r.get("ts") or ""))))
    counts = {str(a["id"]): len(reduce(partial(_no_run_step, a), docs, frozenset())) for a in approved if not a.get("merged")}
    return {task_id: n for task_id, n in counts.items() if n > 0}


def _launched_within_window(ts: object, now: datetime) -> bool:
    """True for an ISO `ts` in the launch budget's window, (now - WINDOW, now]; an unparseable `ts` is outside it."""
    try:
        at = aligned(str(ts), now)
    except ValueError:
        return False
    return now - WINDOW < at <= now


def _run_number(run: str) -> int:
    match = re.search(r"-(\d+)$", run)
    return int(match.group(1)) if match else -1


def launch_history_from_actions(
    actions: Sequence[Row], quarantines: Sequence[QuarantineFacts], now: datetime
) -> dict[str, LaunchHistory]:
    """Per initiative, its done launch_epic and relaunch rows in the last hour and its quarantined runs, newest first.

    A run's `body` and `main_head` are "": no gathered fact carries a ticket body hash or the default-branch head."""
    docs = [_action_doc(row) for row in actions]
    launches = [
        {"at": str(d["ts"]), "kind": d["kind"], "initiative": d.get("initiative") or d.get("target")}
        for d in docs
        if d.get("kind") in ("launch_epic", "relaunch")
        and d.get("status") == "done"
        and (d.get("initiative") or d.get("target"))
        and _launched_within_window(d.get("ts"), now)
    ]
    quarantined = {(q["initiative"], q["run"]): q for q in reversed(quarantines) if q.get("run")}
    initiatives = sorted({x["initiative"] for x in launches} | {i for i, _ in quarantined})

    def runs_of(initiative: str) -> list[RunRecord]:
        own = sorted((r for i, r in quarantined if i == initiative), key=_run_number, reverse=True)
        return [
            {"run_id": r, "quarantined": True, "reason": quarantined[(initiative, r)].get("reason", ""), "body": "", "main_head": ""}
            for r in own
        ]

    return {
        i: {"launches": [{"at": x["at"], "kind": x["kind"]} for x in launches if x["initiative"] == i], "runs": runs_of(i)}
        for i in initiatives
    }


def lease_refusals_from_actions(actions: Sequence[Row], now: datetime) -> dict[str, LeaseRefusalStreak]:
    """Per initiative, its newest unbroken run of lease_held launch rows; rows outside the launch window are dropped first.

    Any later launch_epic or relaunch row with another status ends the run. A row whose reason does not parse is skipped."""
    launches = sorted(
        (
            d
            for d in map(_action_doc, actions)
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

    found = {i: streak_of(i) for i in sorted({d.get("initiative") or d.get("target") for d in launches})}
    return {i: s for i, s in found.items() if s is not None}


def decompose_streak_facts(
    paths: Sequence[str], runs_for: Callable[[str], Sequence[DecomposeRun]]
) -> dict[str, DecomposeStreak]:
    """Intake path to its streak, for only the paths whose trailing empty runs reach the limit."""
    streaks = {path: streak(runs_for(path)) for path in paths}
    return {path: s for path, s in streaks.items() if at_limit(s)}


def read_ticket_items(ws: Path, mode: str) -> list[dict]:
    """Edge. Every ticket under `ws/work`, parsed as the docket reader parses it, its state from the store in "store" mode."""
    return chair_read_docket._work_items(ws, mode)


def read_initiative_repos(ws: Path, home: str) -> dict[str, str]:
    """Edge. Initiative id to the `repo:` in `work/<initiative>/initiative.md` expanded under `home`; an unreadable file or no `repo:` is left out."""
    texts = {p.parent.name: chair_read_docket._text(p) for p in sorted((ws / "work").glob("*/initiative.md"))}
    fields = {name: route.parse_frontmatter(text)[0] for name, text in texts.items() if text is not None}
    return {name: repo_path.expand(f["repo"], home) for name, f in fields.items() if isinstance(f.get("repo"), str) and f["repo"]}


def land_at_facts(cartridge: Mapping[str, Any], values: Mapping[str, str | None], ids: Sequence[str]) -> dict[str, str]:
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
) -> dict[str, Any]:
    """`docket_from_builder` over `items`, each initiative's summary taken under its own land_at.

    `route.initiative_summaries` takes one land_at, so it runs once per initiative. An id missing from `land_at` reads ticket."""
    from core.landing import DEFAULT_LAND_AT  # lazy: core sits beside agent_tools in one venv

    summaries = [
        summary
        for initiative in sorted({i["initiative"] for i in items})
        for summary in route.initiative_summaries(
            [i for i in items if i["initiative"] == initiative], land_at=land_at.get(initiative, DEFAULT_LAND_AT)
        )
    ]
    return chair_read_docket.docket_from_builder(summaries, list(items), busy_lanes, max_in_flight, land_at)


def initiative_facts(
    docket: Row,
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

    def phases_entry(initiative: str) -> dict[str, list[PhaseFacts]]:
        if rows is None or initiative not in rows:
            return {}
        held = {t for i, t in claimed if i == initiative}
        live_here = {p for i, p in live_phases if i == initiative}
        listed = phase_facts(rows[initiative], (land_at or {}).get(initiative, DEFAULT_LAND_AT), held)
        shown = [p for p in listed if p["phase"] not in live_here]
        return {"phases": shown} if shown else {}

    return [
        {
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
            **({"ready_surfaces": list(surfaces.get(i["id"], []))} if surfaces is not None else {}),
            **(
                {"repo": repos[i["id"]], "repo_dir": repo_dir_name(repos[i["id"]])}
                if repos is not None and i["id"] in repos
                else {}
            ),
            **({"land_at": land_at[i["id"]]} if land_at is not None and i["id"] in land_at else {}),
            **phases_entry(i["id"]),
        }
        for i in docket["initiatives"]
        if i["id"] not in live
    ]


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
    return [
        {"initiative": initiative, "run": run, **({"refusal": refusals[run]} if run in refusals else {})}
        for initiative, run in decomposed.items()
        if initiative not in live
        and run not in alive
        and run not in remote_pending
        and item_counts.get(initiative, 1) == 0
        and known.get(initiative) not in {"done", "dropped"}
    ]


def alive_decompose_runs(
    runs_dir: str, decomposed: Mapping[str, str], pid_probe: Mapping[str, PidProbeFact]
) -> set[str]:
    """Edge. The newest decompose runs whose process is alive, probed from `<runs_dir>/<run>.pid` itself.

    Not `epic.run_live`: it returns the lease verdict whenever a lease row exists, so an ended run whose process is
    still writing would read as dead. A missing or unreadable pid file is not alive; so is an empty `runs_dir`."""
    from agent_tools.chair_read_lost import _pid_alive  # lazy: chair_read_lost imports this module

    return {
        run
        for initiative, run in decomposed.items()
        if pid_probe.get(initiative, {}).get("alive")
        or (bool(runs_dir) and _pid_alive(Path(runs_dir) / f"{run}.pid"))
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
    docket: Row,
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
    return {
        "max_in_flight": int(docket["max_in_flight"]),
        "live_runs": live_by_host.get("", 0),
        "hosts": [
            {
                "name": name,
                "live_runs": live_by_host.get(name, 0),
                **({"capacity": caps[name]} if name in caps else {}),
                **({"weight": weights[name]} if name in weights else {}),
                "capabilities": list(abilities.get(name, [])),
            }
            for name in lane_hosts
            if name != local
        ],
        **({"local_lanes": local_lanes} if local_lanes != "any" else {}),
    }


def approved_facts(
    rows: Sequence[Row],
    land_at: Mapping[str, str] | None = None,
    branch_merged: Callable[[Row], bool] | None = None,
) -> list[ApprovedTask]:
    """Each row also carries its initiative's `land_at` (an id missing from `land_at` reads ticket) and `branch_merged`.

    `branch_merged` is asked only for land_at ticket or level, and is given the row with that `land_at` set."""
    def at(r: Row) -> str:
        return (land_at or {}).get(str(r["initiative"]), "initiative")

    def on_branch(r: Row) -> bool:
        return branch_merged is not None and at(r) != "task" and bool(branch_merged({**r, "land_at": at(r)}))

    return [
        {
            "id": r["id"],
            "initiative": r["initiative"],
            "repo": r["repo"],
            "phase": str(r["phase"]),
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
# The cause `apply_release` carries in its note: the ticket's wording, and `chair_approval_hold.plan_release`'s reason text.
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
        {"id": r["initiative"], "task_states": [str(t["state"]) for t in tasks if t["initiative"] == r["initiative"]]}
        for r in rows
        if r.get("kind") == "initiative" and r.get("state") == "todo"
    ]


def epic_facts(rows: Sequence[Row], definitions: Mapping[str, Mapping[str, Any]]) -> list[EpicFacts]:
    """`core.epics.rollup` over the store's work rows; an initiative with no epic is left out.

    Landed is `route.initiative_states`: at least one task row and every one done or dropped."""
    from agent_tools.queue_rows import epic_of_row
    from core.epics import rollup  # lazy: core sits beside agent_tools in one venv

    initiatives = [r for r in rows if r.get("kind") == "initiative"]
    ids = [str(r["initiative"]) for r in initiatives]
    tasks = [r for r in rows if r.get("kind") == "task"]
    landed = {i for i, done in route.initiative_states(ids, tasks).items() if done}
    members = {str(r["initiative"]): epic_of_row(r) for r in initiatives}
    return cast("list[EpicFacts]", rollup(members, landed, definitions))


def phase_branch_facts(rows: Sequence[Row]) -> list[PhaseBranch]:
    """One PhaseBranch per row, in row order; ahead and behind are counts against main."""
    return [
        {
            "initiative": r["initiative"],
            "phase": str(r["phase"]),
            "branch": r["branch"],
            "ahead": int(r["ahead"]),
            "behind": int(r["behind"]),
            "tip": r["tip"],
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
    for ph in sorted(phases, key=lambda p: (p["initiative"], str(p["phase"]))):
        init, phase = ph["initiative"], str(ph["phase"])
        mine = {a["id"]: a for a in approved if a["initiative"] == init and a["phase"] == phase}
        newest = {
            (c["task"], c["run"]): c
            for c in commits
            if c["initiative"] == init and str(c["phase"]) == phase and c["task"] in mine
        }
        rows: list[CarryTask] = sorted(
            (
                {
                    "task": c["task"],
                    "run": c["run"],
                    "host": c["host"],
                    "branch": c["branch"],
                    "commit": c["commit"],
                    "needs": list(mine[c["task"]]["needs"]),
                    "run_seq": int(c["run_seq"]),
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
                    "pending": [t for t in ph["tasks"] if t not in mine],
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
    recorded = row["extra"].get("path")
    return recorded if isinstance(recorded, str) and recorded else f"intake/{row['task_id']}.md"


def intake_paths_from_rows(rows: Sequence[Row], current: str | None = None) -> list[str]:
    """Queued intake rows as workspace-relative paths (`intake/<task_id>.md`), in row order: the shape `read_intake` returns.

    Held intakes are dropped; with `current`, so are those whose `target_release` is above it."""
    return [intake_path_of_row(row) for row in intake_from_rows(rows, current)]


DEFAULT_HOUSEKEEPING_HOURS = 24.0
DEFAULT_STALE_DAYS = 7  # chair.stale_days profile default
DEFAULT_APPROVAL_HOLD_TICKS = 6  # ticks an unmerged approval may be held before the planner releases it
DEFAULT_SHARED_FILE_LIMIT = 2  # chair.max_shared_file_initiatives profile default


def resolve_housekeeping_hours(value: object) -> float:
    """The profile's raw `chair.housekeeping_hours`, or 24 when it is missing, non-numeric, or not positive."""
    try:
        hours = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_HOUSEKEEPING_HOURS
    return hours if hours > 0 else DEFAULT_HOUSEKEEPING_HOURS


def resolve_stale_days(value: object) -> int:
    """The profile's raw `chair.stale_days`, or 7 when it is missing or non-numeric."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_STALE_DAYS


def resolve_approval_hold_ticks(value: object) -> int:
    """The raw approval hold limit, or 6 when it is missing, non-numeric, or not positive."""
    try:
        ticks = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_APPROVAL_HOLD_TICKS
    return ticks if ticks > 0 else DEFAULT_APPROVAL_HOLD_TICKS


def resolve_max_launches_per_hour(value: object) -> int:
    """The profile's raw `chair.max_launches_per_hour`, or the default when it is missing, non-integer, or not positive."""
    try:
        limit = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_LAUNCHES_PER_HOUR
    return limit if limit > 0 and not isinstance(value, bool) else DEFAULT_MAX_LAUNCHES_PER_HOUR


def resolve_shared_file_limit(value: object) -> int:
    """A finite number of at least 1 (a bool is not a number) as an int, else the default. inf and nan parse from a profile."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value < math.inf:
        return DEFAULT_SHARED_FILE_LIMIT
    return int(value)


def resolve_serial_files(value: object) -> list[str]:
    """The string entries of a list; anything else is no serial paths."""
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []


def resolve_idle_stall_minutes(value: object) -> int:
    """The profile's raw `chair.idle_stall_minutes`, or 15 when it is missing, non-numeric, or not positive."""
    try:
        minutes = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return IDLE_STALL_MINUTES
    return minutes if minutes > 0 else IDLE_STALL_MINUTES


def resolve_steward_enabled(value: object) -> bool:
    """The profile's raw steward setting: on only for boolean True or the string "true", else off."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def resolve_steward_scope(value: object) -> str:
    """The profile's raw steward_scope: "epics" only for that string, case and space ignored, else "all"."""
    return "epics" if isinstance(value, str) and value.strip().lower() == "epics" else "all"


def steward_initiative_ids(scope: str, epics: Sequence[EpicFacts], rows: Sequence[Row]) -> list[str]:
    """Sorted ids the steward manages. "epics": unlanded epic members. Otherwise every initiative row not landed.

    Landed is `route.initiative_states`: at least one task row and every one done or dropped."""
    if scope == "epics":
        return sorted({i["id"] for e in epics for i in e["initiatives"] if not i["landed"]})
    ids = sorted({str(r["initiative"]) for r in rows if r.get("kind") == "initiative"})
    tasks = [r for r in rows if r.get("kind") == "task"]
    landed = {i for i, done in route.initiative_states(ids, tasks).items() if done}
    return [i for i in ids if i not in landed]


_STEWARD_STATES = ("ready", "approved", "done", "quarantined", "dropped", "building")


def _steward_task(row: Row) -> StewardTask:
    """A store state the steward does not know (todo, blocked) is open work it must not relaunch, so building."""
    state = str(row["state"])
    return {"id": str(row["task_id"]), "state": state if state in _STEWARD_STATES else "building"}  # type: ignore[typeddict-item]


def _natural(phase: str) -> list[object]:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", phase)]


def steward_plan_facts(
    initiatives: Sequence[str],
    rows: Sequence[Row],
    run_exited: Mapping[str, bool],
    heads: Callable[[str], str],
    level_merged: Callable[[str, str], bool],
    entries: Sequence[Row],
    land_at: Mapping[str, str] | None = None,
    landed: Mapping[str, Collection[str]] | None = None,
) -> StewardPlanFacts:
    """One StewardFacts per initiative. A level is a phase of its task rows, in natural phase order.

    `land_at` is `land_at_facts` output; an id missing from it reads ticket. At ticket a level is merged by
    `level_merged` (ancestry). At level and task it is merged when every non-dropped task is landed and done or merged."""
    from core.landing import DEFAULT_LAND_AT  # lazy: core sits beside agent_tools in one venv

    tasks = [r for r in rows if r.get("kind") == "task"]

    def build(initiative: str) -> StewardFacts:
        own = [r for r in tasks if r["initiative"] == initiative]
        phases = sorted({str(r["phase"]) for r in own}, key=_natural)
        mode = (land_at or {}).get(initiative, DEFAULT_LAND_AT)
        done = set((landed or {}).get(initiative, ()))

        def merged(p: str) -> bool:
            if mode == DEFAULT_LAND_AT:
                return level_merged(initiative, p)
            # `landed` counts an approved task as met at level whether or not it merged, so merged-ness is checked on the row.
            return all(
                r["task_id"] in done and (r["state"] == "done" or route.is_merged(r))
                for r in own
                if str(r["phase"]) == p and r["state"] != "dropped"
            )

        levels = [
            {"tasks": [_steward_task(r) for r in own if str(r["phase"]) == p], "merged": merged(p), "phase": p}
            for p in phases
        ]
        return {
            "epic_id": initiative,
            "branch_head": heads(initiative),
            "levels": levels,
            "exited": bool(run_exited.get(initiative, False)),
            "signature": task_signature([_steward_task(r) for r in own]),
            "reported": reported_keys(entries, initiative),
            "land_at": mode,
        }

    return {"enabled": True, "epics": [build(i) for i in initiatives]}


def idle_stall_inputs(
    backlog: Row,
    hosts: Sequence[HostCheck],
    lands_waiting: Sequence[LandWait],
    open_item: tuple[str | None, str | None],
    stall_minutes: int,
) -> IdleStallInputs:
    """The four idle readers' outputs as one input. `open_item` is `read_idle_open`'s (signature, diagnosis)."""
    return {
        "free_lanes": int(backlog["free_lanes"]),
        "ready": int(backlog["ready"]),
        "queued": int(backlog["queued"]),
        "last_progress_at": backlog["last_progress_at"],
        "stall_minutes": stall_minutes,
        "hosts": list(hosts),
        "empty_stubs": list(backlog["empty_stubs"]),
        "lands_waiting": list(lands_waiting),
        "blocked_ready": list(backlog["blocked_ready"]),
        "open_signature": open_item[0],
        "open_diagnosis": open_item[1],
    }


def _smoke_of(watch: LandWatch, hold: HoldRecord | None) -> tuple[str, str]:
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
        return {  # type: ignore[typeddict-item]
            **watch, "ci": ci, "ci_output": ci_output, "smoke": smoke, "smoke_output": smoke_output, "green_after": green_after,
        }

    return [build(w) for w in watches]


def gather_facts(
    deps: FactsDeps, now: datetime, epic_definitions: Mapping[str, Mapping[str, Any]] | None = None
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
    if deps.landing_records is not None and items:
        # The one place the merged flag takes its landing-model meaning for the tick: everything below reads `items`.
        item_land_at = land_at_facts(deps.cartridge(), deps.land_at_values(), sorted({str(i["initiative"]) for i in items}))
        items = with_landing_merged(items, list(deps.landing_records()), item_land_at)
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
    leased = {(r["initiative"], r["task_id"]) for r in rows if r["kind"] == "task" and chair_read_docket._claimed(r, now_iso)}
    initiatives = [
        {**i, "dag_problem": problems[i["id"]]} if i["id"] in problems else i
        for i in initiative_facts(
            {"initiatives": ready}, live,
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
    quarantined = quarantined_from_rows(rows) if rows else list(deps.quarantined())
    stranded_records = deps.stranded_records() if deps.stranded_records is not None else None
    stranded = stranded_from_rows(rows, list(stranded_records)) if rows and stranded_records is not None else list(deps.stranded())
    current = deps.current_version() if rows else None  # file mode carries no gate rows, so it reads no version
    intake = intake_paths_from_rows(rows, current) if rows else list(deps.intake())
    # Store mode drops stranded rows for tasks the store does not hold approved (see `store_approved_tasks`, which
    # also names the gap). File mode carries no store state and filters nothing.
    quarantine = quarantine_facts(
        quarantined, stranded, deps.attempts(), live, deps.has_patch, planned_tasks(approved, initiatives),
        store_approved_tasks(rows) if rows else None,
        waiting={(a["initiative"], a["id"]) for a in approved},
    )
    action_rows = list(deps.actions()) if deps.actions is not None else []
    login_hosts = deps.hosts()
    decomposed = dict(deps.decomposed_intake()) if deps.decomposed_intake is not None else {}
    probe = dict(deps.pid_probe())
    unfetched = deps.remote_unfetched() if deps.remote_unfetched is not None else {}
    from agent_tools.chair_decompose_runs import decompose_refusals  # lazy: chair_decompose_runs imports this module

    refusals = land_refusals_from_actions(action_rows, now)
    work = list(deps.work_rows()) if deps.work_rows is not None else []
    epics = epic_facts(work, epic_definitions or {}) if deps.work_rows is not None else []
    steward_ids = (
        steward_initiative_ids(resolve_steward_scope(deps.steward_scope()), epics, work)
        if resolve_steward_enabled(deps.steward_enabled())
        else None
    )
    return {
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
        "run_exited": dict(deps.run_exited()) if deps.run_exited is not None else {},
        "remote_unfetched": dict(unfetched),
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
        "decompose_streaks": decompose_streak_facts(intake, deps.decompose_runs) if deps.decompose_runs is not None else {},
        "decompose_stalled_reported": decompose_stalled_keys(action_rows),
        "env_stale_reported": stale_checkout_keys(action_rows),
        "held_files": [dict(h) for h in deps.held_files()],  # type: ignore[misc]
        "held_reported": held_file_keys(action_rows),
        "held_initiatives": sorted(held_initiatives(rows)) if rows else [],
        "deferred_initiatives": deferred_initiatives(rows, current) if rows and current is not None else {},
        "host_env": host_env_from_rows(login_hosts),
        "relaunch_loop_reported": relaunch_loop_keys(action_rows),
        "land_refusals": refusals,
        "land_conflicts": read_land_conflicts(deps.runs_dir(), repos),
        "land_heads": read_land_heads(refusals, repos, deps.rev_parse),
        **({"land_freeze": freeze} if (freeze := land_freeze_fact(deps.runs_dir(), now)) is not None else {}),
        **(
            {"merge_queue": queue}
            if (queue := merge_queue_fact(deps.merge_queue() if deps.merge_queue is not None else None)) is not None
            else {}
        ),
        **(
            {"host_load_ceiling": ceiling}
            if (ceiling := resolve_load_ceiling(deps.host_load_ceiling() if deps.host_load_ceiling is not None else None))
            is not None
            else {}
        ),
        "launch_history": launch_history_from_actions(action_rows, quarantine, now),
        "lease_refusals": lease_refusals_from_actions(action_rows, now),
        "max_launches_per_hour": resolve_max_launches_per_hour(
            deps.max_launches_per_hour() if deps.max_launches_per_hour is not None else None
        ),
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
        **({"phase_records": list(deps.phase_records(approved))} if deps.phase_records is not None else {}),
        "running": (
            running_initiatives(live, items, repos, unlanded=unfinished, live_phases=live_phase_pairs)
            if deps.tickets is not None
            else []
        ),
        **(
            {"live_phases": [LivePhase(initiative=i, phase=p) for i, p in live_phase_pairs]}
            if deps.live_phases is not None
            else {}
        ),
        **(
            {
                "schedule_inputs": schedule_inputs_from_rows(
                    items, work, action_rows, dispatch,
                    local=deps.host, repos=repos,
                    land_at=land_at_facts(deps.cartridge(), deps.land_at_values(), sorted({str(i["initiative"]) for i in items})),
                    live_phases=live_phase_pairs,
                    newest_run_host=newest_run_host,
                )
            }
            if deps.tickets is not None
            else {}
        ),
        "steer_streaks": steer_streaks_from_actions(action_rows),
        "land_fail_streaks": land_fail_streaks_from_actions(action_rows),
        "no_run_streaks": no_run_streaks_from_actions(approved, action_rows),
        "approval_holds": approval_holds_from_actions(approved, action_rows),
        "wrongly_released": list(deps.wrongly_released()),
        "approval_hold_ticks": resolve_approval_hold_ticks(deps.approval_hold_ticks())
        if deps.approval_hold_ticks is not None
        else DEFAULT_APPROVAL_HOLD_TICKS,
        "steer_policy": {
            "limit": resolve_shared_file_limit(deps.shared_file_limit() if deps.shared_file_limit is not None else None),
            "serial": resolve_serial_files(deps.serial_files() if deps.serial_files is not None else None),
        },
        "waiting_reported": waiting_reported_from_actions(action_rows),
        "pull_age_s": pull_age_s(action_rows, now),
        "stranded": stranded_facts(approved, list(deps.run_commits()), list(deps.phase_state())),
        "phase_branches": phase_branch_facts(list(deps.branch_counts())),
        "landing": list(deps.landing()) if deps.landing is not None else [],
        "idle_stall": deps.idle_stall(now),
        "tuning": tuning_facts(
            *deps.tuning(now),
            weekly_fraction(weekly),
            pacing._elapsed_fraction(weekly, now) if weekly is not None else 0.0,
            current_lanes(dispatch), dict(deps.lane_bounds()),
        ),
        "landed_main": landed_main_facts(list(deps.land_watches()), deps.read_main_ci, deps.land_hold(), deps.green_since),
        "land_outcomes": dict(deps.land_outcomes()),
        "handoff_stops": list(deps.handoff_stops()),
        "queue_files": deps.queue_files(),
        "epics": epics,
        **(
            {
                "steward": steward_plan_facts(
                    steward_ids,
                    work,
                    dict(deps.run_exited()) if deps.run_exited is not None else {},
                    deps.ticket_head,
                    deps.level_merged,
                    list(deps.steward_entries()),
                    land_at_facts(deps.cartridge(), deps.land_at_values(), steward_ids),
                    # An initiative missing from `initiatives` (a live run) has no landed set and reads not merged.
                    {i["id"]: i["landed"] for i in initiatives},
                )
            }
            if steward_ids is not None
            else {}
        ),
    }
