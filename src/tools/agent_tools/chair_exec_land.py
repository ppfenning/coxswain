"""The chair's executor, landing: results, deps, land and carry actions, clears, merges, launches and fetches."""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from agent_tools import (
    base_branch,
    chair_apply_fetch,
    chair_apply_widen,
    chair_lease_refusal,
    chair_plan_prune,
    chair_sort,
    courier,
    epic,
    partial_land,
    queue_export,
    queue_rows,
    remote_lane,
    repo_path,
    route,
    route_guard,
    run_store,
    store_cli,
)
from agent_tools.chair_carry_exec import ForgePort, GitPort, StorePort
from agent_tools.chair_phase_quarantine import Quarantined
from agent_tools.chair_read_land_conflicts import rev_parse_commit as rev_parse
from agent_tools.chair_rebase_exec import RebasePort
from agent_tools.chair_revert_exec import ForgePort as RevertForgePort
from agent_tools.chair_revert_exec import GitPort as RevertGitPort
from agent_tools.chair_types import Action, ActionKind
from agent_tools.remote_argv import ssh_argv, sync_argv

Status = Literal[
    "fenced", "dry_run", "skipped", "refused", "recorded", "done", "failed", "landed", "not_landed", "escalated", "busy",
    "in_progress", "lease_held", "deferred",
]

Run = Callable[[list[str]], tuple[int, str]]
Fetch = Callable[[list[str]], tuple[int, str] | None]  # None: the host was not reached, so its fact is left out
NOT_PROBED = "remote lanes not probed (dry run)"  # the line a dry run's status should carry for its remote-lane facts


class Result(TypedDict):
    action: Action
    status: Status
    reason: str
    run: NotRequired[str]  # a fetch_exit's run, for the next task's status line
    host: NotRequired[str]  # a fetch_exit's remote host, read from its <run>.remote.json
    needs_chair: NotRequired[Action]  # a refused land's classified needs_chair, escalated in place of the generic one;
    # or a clear_branches carry's conflicted-merge needs_chair, recorded alongside its own done result
    commit: NotRequired[str]  # a landed land's origin/main commit, from land_commit; set only when landed() was True
    landed_commits: NotRequired[list[partial_land.LandedCommit]]  # a partial_land's every landed task commit, any status
    already_landed: NotRequired[bool]  # a land_phase whose phase branch is gone with every task done; never escalated


class LandSink(Protocol):
    """Where a tick hands a land and moves on; a repository runs up to its limit of lands, and each result comes back through `collect`."""

    def pending(self, repo: str) -> bool:
        """True while a land handed over for `repo` has not been collected."""
        ...

    def full(self, repo: str) -> bool:
        """True while `repo` has as many uncollected lands as its limit admits."""
        ...

    def landing(self) -> list[dict[str, str]]:
        """One dict per land in progress, with its `initiative`, `phase` and `repo`."""
        ...

    def submit(self, action: Action, work: Callable[[], Result]) -> Result:
        """Start `work` in the background and return at once: `in_progress`, or `busy` when another process holds the repo."""
        ...

    def start(self, action: Action, argv: list[str]) -> Result:
        """Start the land as its own process and return `in_progress` at once. Optional: a sink without it takes `submit`."""
        ...

    def collect(self) -> list[Result]:
        """The lands that finished since the last call, each once."""
        ...


@dataclass(frozen=True)
class Refusal:
    reason: str


def _no_intake_id(path: str) -> str:
    return ""


def _no_initiative_rows() -> tuple[()]:
    return ()


@dataclass(frozen=True)
class Deps:
    run: Run  # (exit code, output); the only door to cox and git
    delete_branches: Callable[[str, str], tuple[list[str], str]]  # (repo, pattern) -> (deleted names, error text)
    acquire_lease: Callable[..., str]  # (holder, host), (holder, host, steal=True) over an expired takeover, or (holder, host, steal=True, takeback=True) over a session -> refusal line or ""
    record: Callable[[Action], None]
    run_id: Callable[[Action], str]  # the run whose task a land applies to; "" when unknown
    repo_for: Callable[[Action], str]  # the repository a clear_branches acts in; "" when unknown
    ssh_for: Callable[[str], str] = lambda host: ""  # a lane host's name -> its ssh destination; "" when the hosts table has none
    intake_id: Callable[[str], str] = _no_intake_id  # an intake path -> the id in its frontmatter; "" when it has none
    initiative_rows: Callable[[], Sequence[Mapping]] = _no_initiative_rows  # queue rows, read at the edge
    runs_dir: Path = field(default_factory=lambda: Path("."))  # a fetch_exit's task records and remote record live here
    work_dir: Path = field(default_factory=lambda: Path("."))  # a fetch_exit's ticket files live under work_dir/work/<initiative>
    now: Callable[[], str] = lambda: datetime.now(UTC).isoformat()  # the tick's clock, passed to plan_stale_draft
    # The courier `to` of a stale_to_draft note. docs/design/courier.md's "Who writes" names "the
    # steward's proposals" as an existing writer to copy the recipient from, but no such call exists:
    # courier.send has exactly one call site before this patch (cli.py's generic `cox courier send`,
    # whose `to` comes from the caller's `--to` flag), and neither steward.py nor steward_draft.py
    # imports courier at all. "chair" reuses courier.inbox's own generic label instead (see its
    # docstring): it reaches whichever chair holds the lease, and bare `cox` prints that holder's
    # inbox to the human at start. A session label such as "chair-2026-09-27" would reach only that
    # one session, so the generic label is used here.
    note_to: str = "chair"
    check_login: Callable[[str], dict] | None = None  # a check_login's host name -> the hosts row cox host beat prints
    probe_host: Callable[[str], dict] | None = None  # a probe_host's host name -> the hosts row, or {}
    log_retention_days: int = 7  # the profile's log_retention_days: traces and run logs kept locally, in days
    launch_stagger_s: float = 20.0  # the profile's chair.launch_stagger_s: seconds between two launches on one host in a tick; 0 is none
    harness_python: str = "python"  # the interpreter the trace prune runs under; the harness venv's python when one is configured
    send_signal: Callable[[int, int], None] = os.kill  # a stalled_usr1/stalled_stop's (pid, signal) call; the only door to os.kill
    ids_mode: str = "slug"  # the routing profile's `ids:` key; "sequence" makes a launch_decompose carry --initiative-id/--task-ids
    lands: LandSink | None = None  # None runs a land in line, as before; a sink runs it behind the tick
    carry_ports: Callable[[Action], tuple[GitPort, ForgePort, StorePort]] | None = None  # a carry_phase's ports; None refuses it
    rebase_port: Callable[[Action], RebasePort] | None = None  # a rebase_phase's port; None refuses it
    set_lanes: Callable[[str, int], int] | None = None  # a tune_lanes's (host, count) -> exit code; None refuses it
    revert_ports: Callable[[Action], tuple[RevertGitPort, RevertForgePort]] | None = None  # a revert_land's ports; None refuses it
    home: str = ""  # this machine's home directory, read once at the edge; "" leaves a repo path unmapped for a lane host
    quarantine_phase: Callable[[str, str, str, str], Quarantined | None] | None = None  # (initiative, phase, cause, reason): quarantines that phase's tasks, answering those it moved and one line per write the store refused
    resolve_land: Callable[[str, str, str], None] | None = None  # (initiative, phase, outcome): the store's resolve write for a land
    rerun_main: Callable[[str, str], tuple[str, str]] | None = None  # a rerun_main's (repo, commit) -> chair_revert_ports.rerun_infra's (state, line); None refuses it
    rev_parse: Callable[[str, str], str | None] = rev_parse  # (repo, ref) -> the ref's tip, None when it does not resolve; the only door to a land_phase's head
    phase_states: Callable[[str, str], list[str]] = lambda initiative, phase: []  # (initiative, phase) -> the store state of each of its tasks; [] when unknown
    label: str = ""  # the chair's own holder label; each guarded cox command it shells out to carries it as --label
    target_branch: Callable[[str], str | None] | None = None  # initiative -> the branch it lands on; None or a None result keeps the repo default
    temp_root: Path | None = None  # the system temp directory the tick sweeps stale pytest run directories under; None sweeps nothing
    user: str = ""  # the user whose `pytest-of-<user>` directory the sweep reads
    # An annotate_objection's cheap model step. None refuses it, which publishes needs_attention. The planner no longer
    # plans annotate_objection: a failure that needs a tweak is triaged by the model in the next run (request_triage).
    objection_note: chair_sort.ObjectionNote | None = None
    mark_handled: Callable[[str], None] | None = None  # a handled key's extra sink; the recorded action already carries `key`
    # An intake path -> a digest that changes when the intake does (its file, its store row, its path); a decompose
    # launch that found no idea file records it, so the planner can tell a repeat on an unchanged intake from a fixed one.
    # None when it cannot be read this tick (a failed store read): the miss is then recorded with no fingerprint,
    # which the facts read as no change to the backoff.
    intake_fingerprint: Callable[[str], str | None] = _no_intake_id


def resolved_target(cartridge_target: str | None, epic_branch: str | None, initiative_branch: str | None) -> str | None:
    """Pure. The initiative's value, else its epic's, else the cartridge's; None when none is set."""
    from core.landing import resolve_target_branch  # lazy: core sits beside agent_tools in one venv

    return resolve_target_branch({"landing_areas": {"target_branch": cartridge_target}}, epic_branch, initiative_branch)


class TargetUnreadable(RuntimeError):
    """The initiative's target branch cannot be told. A step that meets it refuses: landing on the default would be a wrong merge."""


def initiative_row(initiative: str, rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Pure. The queue row of kind initiative named `initiative`, else None."""
    return next((r for r in rows if r.get("kind") == "initiative" and r.get("initiative") == initiative), None)


def target_for(
    initiative: str,
    rows: Sequence[Mapping[str, Any]],
    epics: Mapping[str, Mapping[str, Any]],
    cartridge_target: str | None,
) -> str | None:
    """Pure. The branch `initiative` lands on, from its queue row and its epic's definition."""
    row = initiative_row(initiative, rows)
    epic = queue_rows.epic_of_row(row) if row is not None else None
    initiative_branch = queue_rows.target_branch_of_row(row) if row is not None else None
    epic_branch = (epics.get(epic) or {}).get("target_branch") if epic is not None else None
    return resolved_target(cartridge_target, epic_branch, initiative_branch)


def target_branch_of(deps: Deps, initiative: str) -> str | None:
    """The branch `initiative` lands on, or None for the repo default. A caller's stub with no `target_branch` has none."""
    read = getattr(deps, "target_branch", None)
    return read(initiative) if read is not None and initiative else None


LAUNCH_KINDS = ("relaunch", "retry", "launch_epic", "launch_decompose", "rescue")
DEFAULT_LAUNCH_STAGGER_S = 20.0


def stagger_seconds(value: object) -> float:
    """The profile's `chair.launch_stagger_s`; the default for a missing, non-numeric, negative or non-finite value. 0 means no stagger."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return DEFAULT_LAUNCH_STAGGER_S
    try:
        seconds = float(value)
    except ValueError:
        return DEFAULT_LAUNCH_STAGGER_S
    return seconds if math.isfinite(seconds) and seconds >= 0 else DEFAULT_LAUNCH_STAGGER_S


UNFENCED_KINDS = ("standby", "take_lease", "steer_clear", "waiting", "deferred", "unlaunched")  # not writes, so a stale or missing epoch does not stop them
LAND_ACTION_KINDS = ("land", "land_phase", "partial_land")  # a partial_land is a run of task lands, blocked and escalated as one
_REASON_CAP = 600
LOCAL_ARGV_TIMEOUT_S = 900.0  # a local argv with no bound of its own; matches _CHECK_COMMAND_TIMEOUT_S
LAND_TIMEOUT_S = 45 * 60.0  # the overall bound on one `cox runs land`; it outlives the checks it runs
TIMED_OUT = 124  # the exit code `run_argv` reports for a child it killed on its timeout
_GLOB_CHARS = frozenset("*?[]{}\\ \t")
# Every `cox runs land` refusal starts "land: refusing, ": a dirty repo, a branch conflict, a forge mismatch.
# Only the repo-lease line also says "<pid> on <host> is landing in <repo>", and only that one is worth retrying.
_REFUSAL_PREFIX = "land: refusing, "
_BUSY_MARK = " is landing in "
# `cox runs land --phase` prints `land: no phase record at <path>` (cli.py) for a run with no phase record; a land by
# task needs none, so this refusal alone falls back to the phase's task lands.
_NO_PHASE_RECORD = "land: no phase record at "


def _repo_busy(output: str) -> bool:
    """True when a line of the land's output is the repo-lease refusal: another land holds this repository."""
    return any(line.startswith(_REFUSAL_PREFIX) and _BUSY_MARK in line for line in output.splitlines())


def lacks_phase_record(code: int, output: str) -> bool:
    """True when the land exited non-zero and a line of its output is the missing-phase-record refusal."""
    return code != 0 and any(line.startswith(_NO_PHASE_RECORD) for line in output.splitlines())


# The line a phase land prints for a skipped merge; `cli.ALREADY_MERGED_PREFIX` is the same text, pinned by a test.
_ALREADY_MERGED_PREFIX = "merge: already merged"
_LANDED_VIA_PREFIX = "landed via"


def landed(code: int, output: str) -> bool:
    """A land counts only on exit 0 with `mark_done:` and one of `merge:`, the already-merged line or `landed via`."""
    skipped = any(line.startswith(_ALREADY_MERGED_PREFIX) for line in output.splitlines())
    landed_via = any(line.startswith(_LANDED_VIA_PREFIX) for line in output.splitlines())
    return code == 0 and "mark_done:" in output and ("merge:" in output or skipped or landed_via)


_TICKET_GRANULARITY_NOTE = "ticket granularity, the one pull request opens at the last phase"  # land.py plans it, cli._land_walk prints it, test_land_granularity.py pins it; change them together


def note_only_land(code: int, output: str) -> bool:
    """True on exit 0 when the output carries the ticket-granularity note and no step line: nothing landed, nothing failed."""
    steps = any(mark in output for mark in ("mark_done:", "merge:", _LANDED_VIA_PREFIX)) or land_stop(output) is not None
    return code == 0 and not landed(code, output) and _TICKET_GRANULARITY_NOTE in output and not steps


# The old "exactly one commit ahead" wording stays: logs written before land_plan changed still carry it.
_MISSING_BRANCH_MARKERS = (
    "no branch is ahead of",
    "no branch is exactly one commit ahead of",
    "no candidate branch found for",
)


def land_refusal(action: Action, code: int, output: str) -> Action | None:
    """None on a clean exit; else the needs_chair a land's refusal raises, its cause read from `output` by
    literal substring, else the land fallback.

    "could not apply" is git cherry-pick's own conflict line (its stderr on a conflict, verified against
    `_execute_land_step`'s cherry_pick step); "CONFLICT" never appears there because that step keeps stderr
    over stdout, and cherry-pick writes its "CONFLICT (content): ..." line to stdout, not stderr.
    """
    if code == 0:
        return None
    if "could not apply" in output:
        cause = "conflict"
    elif "failing checks:" in output:
        cause = "checks"
    elif any(marker in output for marker in _MISSING_BRANCH_MARKERS):
        cause = "missing_branch"
    else:
        cause = "land"
    return {
        "kind": ActionKind.NEEDS_CHAIR, "initiative": action.get("initiative", ""), "task_id": action.get("task_id", ""),
        "cause": cause, "epoch": action.get("epoch", 0), **phase_action(action),
    }


def phase_action(action: Action) -> Action:
    """`{"phase": ...}` for a land_phase, which carries no task_id, so its needs_chair still names what failed; else empty."""
    phase = action.get("phase", "")
    return {"phase": phase} if phase else {}


def land_subject(action: Action) -> str:
    """The task a land names, else `phase <phase>` for a land_phase; the name a skip or escalation reason quotes."""
    task, phase = action.get("task_id", ""), action.get("phase", "")
    return task if task else (f"phase {phase}" if phase else "")


@dataclass(frozen=True)
class Blocker:
    """The uncounted land that blocks a repo or initiative: the label a skip prints, and the initiative, phase and
    step (empty when its output named none) that decide whether a later land may resume."""

    label: str
    initiative: str = ""
    phase: str = ""
    step: str = ""


def branch_pattern(initiative: str) -> str | None:
    """`epic/<initiative>/*`, or None when the initiative is empty or could widen the glob."""
    if not initiative or "/" in initiative or _GLOB_CHARS & set(initiative):
        return None
    return f"epic/{initiative}/*"


def decompose_id(path: str, file_id: str) -> str | Refusal:
    """The initiative id for an intake: its frontmatter id, else the file stem. Never the path; a path-shaped id is refused."""
    chosen = file_id or Path(path).stem
    if not chosen or "/" in chosen or "\\" in chosen or chosen.startswith("."):
        return Refusal(f"intake {path!r} gives initiative id {chosen!r}, which is empty or path-shaped")
    return chosen


def tail(text: str, limit: int) -> str:
    """The last whole lines of `text` that fit in `limit` characters; a single line longer than `limit` keeps its end."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    window = text[-limit:]
    if text[-limit - 1] == "\n":
        return window
    _, newline, rest = window.partition("\n")
    return rest if newline and rest.strip() else window


def failed_reason(stdout: str, stderr: str, cap: int = _REASON_CAP) -> str:
    """The first non-empty stdout line (cox's verdict, at most half of `cap`), then the last whole stderr lines; at most `cap` characters."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    errors = stderr.rstrip()
    if lines and errors:
        verdict = lines[0][: cap // 2]
        return f"{verdict}\n{tail(errors, cap - len(verdict) - 1)}"
    elif lines:  # noqa: RET505 - every branch spelled out, each with its else
        return lines[0][:cap]
    elif errors:
        return tail(errors, cap)
    else:
        return ""


def argv_for(action: Action, initiative_id: str = "", ids_mode: str = "slug", label: str = "") -> list[str] | None:
    """The cox argv for a land, fetch, launch or pull action; None for any other kind or a missing required field.

    A decompose launch needs `initiative_id`, which `decompose_id` derives from the intake; it is never taken from the path.
    `ids_mode` is the routing profile's `ids:` key: "sequence" appends `--task-ids ordinal` so the graph's own
    tasks come out named `<initiative_id>-t1`, `<initiative_id>-t2`, ...; "slug" (or anything else) appends neither
    flag, today's behaviour.
    `label` is the chair's own holder label. A land or launch is guarded by the loop lock and its subprocess has no
    COX_SESSION_LABEL, so without `--label` it reads as unlabeled and the chair's own live lock refuses it.
    """
    kind = action.get("kind")
    tag = ["--label", label] if label else []
    # The loop already holds the chair: a command it starts must never claim the loop lease itself (a land that
    # found the record stale took it with an empty host on 2026-09-27 and put the loop in standby).
    task, repo, initiative = action.get("task_id", ""), action.get("repo", ""), action.get("initiative", "")
    run = action.get("run", "")
    phase = action.get("phase", "")
    idea = (action.get("intake_ids") or [""])[0]
    if kind == "land":
        return ["cox", "runs", "land", run, "--task", task, "--repo", repo, "--apply", "--no-claim", *tag] if run and task and repo else None
    if kind == "land_phase":
        return ["cox", "runs", "land", run, "--phase", phase, "--repo", repo, "--apply", "--no-claim", *tag] if run and phase and repo else None
    if kind in ("fetch", "fetch_exit"):
        return ["cox", "runs", "fetch", run] if run else None
    if kind == "launch_decompose":
        task_ids = ["--task-ids", "ordinal"] if ids_mode == "sequence" else []
        decompose_host = action.get("host", "")
        return [
            "cox", "route", "launch", "decompose", "--idea", idea, "--initiative-id", initiative_id, *task_ids, "--no-claim", *tag,
            *(["--on", decompose_host] if decompose_host else []),
        ] if idea and initiative_id else None
    if kind == "rescue":
        return ["cox", "route", "launch", "rescue", "--initiative", f"work/{initiative}", "--task", task, "--no-claim", *tag] if initiative and task else None
    if kind in LAUNCH_KINDS:
        host = action.get("host", "")
        return [
            "cox", "route", "launch", "epic", "--initiative", f"work/{initiative}", "--no-claim", *tag,
            *(["--repo", repo] if repo else []), *(["--on", host] if host else []),
        ] if initiative else None
    if kind == "pull":
        return ["cox", "route", "pull"]
    if kind in ("steer_clear", "waiting", "deferred", "unlaunched"):  # recorded only; no command runs
        return None
    return None


def make_result(action: Action, status: Status, reason: str = "") -> Result:
    return {"action": action, "status": status, "reason": reason}


def recorded_action(result: Result) -> Action:
    """The action with its outcome attached; the reason keeps its last 600 characters, where a traceback's failing frame is."""
    return {**result["action"], "status": result["status"], "reason": tail(result["reason"], _REASON_CAP)}  # type: ignore[typeddict-item]


def _land_result(action: Action, repo: str, code: int, output: str) -> Result:
    """Same as `make_result(action, "landed"/"not_landed", output)`, but a true `landed()` also carries the repo's
    new origin/main commit from `land_commit`, run right after so nothing else has moved origin/main yet."""
    if not landed(code, output):
        return make_result(action, "not_landed", output)
    return {**make_result(action, "landed", output), "commit": land_commit(repo)}


def _run_land(deps: Deps, argv: list[str]) -> tuple[int, str]:
    """`deps.run`, with a `TimeoutExpired` from a door that does not catch it read as the timed-out exit."""
    try:
        return deps.run(argv)
    except subprocess.TimeoutExpired:
        return TIMED_OUT, ""


def _land_timed_out(action: Action, argv: list[str]) -> Result:
    """The failure a land records when `cox runs land` outlives LAND_TIMEOUT_S; the reason names the command and the bound."""
    return make_result(action, "failed", f"{' '.join(argv)}: timed out after {int(LAND_TIMEOUT_S // 60)} min")


def runs_as_process(action: Action) -> bool:
    """A partial_land, and a land_phase with task_runs, can fall back to per-task lands, so they stay on the thread path."""
    kind = action.get("kind")
    return kind == "land" or (kind == "land_phase" and not action.get("task_runs"))


def land_command(action: Action, deps: Deps, blocked: dict[str, Blocker]) -> tuple[Action, list[str]] | Result:
    """The start half of a land: (action, argv), or the finished Result of an early exit. A land_phase action
    comes back with its `head` applied, and that is the action `land_outcome` must be given."""
    repo = action.get("repo", "")
    phase = action.get("kind") == "land_phase"
    argv = argv_for(action, label=deps.label)
    if argv is None:
        need = "land_phase needs a run id, a phase and a repo" if phase else "land needs a run id, a task_id and a repo"
        return make_result(action, "refused", need)
    if repo in blocked:
        return make_result(action, "skipped", f"an earlier land in {repo} ({blocked[repo].label}) was not counted")
    return (_with_head(action, deps, repo) if phase else action), argv


def land_outcome(action: Action, deps: Deps, code: int, output: str) -> Result:
    """The classify half of a land: the Result for a finished command's exit code and output."""
    argv = argv_for(action, label=deps.label) or []
    if code == TIMED_OUT:
        return _land_timed_out(action, argv)
    # Busy first: a repo-lease refusal exits non-zero like every other refusal, so `land_refusal` would escalate it.
    if _repo_busy(output):
        return make_result(action, "busy", output)
    if action.get("kind") == "land_phase":
        gone = _already_landed_result(action, deps, output)
        if gone is not None:
            return gone
    refusal = land_refusal(action, code, output)
    if refusal is not None:
        return {"action": action, "status": "refused", "reason": output, "needs_chair": refusal}
    if action.get("kind") == "land_phase" and note_only_land(code, output):
        return make_result(action, "deferred", output.strip())
    return _land_result(action, action.get("repo", ""), code, output)


def perform_land(action: Action, deps: Deps, blocked: dict[str, Blocker]) -> Result:
    command = land_command(action, deps, blocked)
    if not isinstance(command, tuple):
        return command
    action, argv = command
    code, output = _run_land(deps, argv)
    return land_outcome(action, deps, code, output)


def perform_land_async(action: Action, deps: Deps, sink: LandSink) -> Result:
    """Hand the land to the sink and return at once; a repo already at its land limit is left alone.

    A sink with `start` runs a `land` or a `land_phase` without task_runs as its own process; any other land goes to `submit`."""
    repo = action.get("repo", "")
    if sink.full(repo):
        return make_result(action, "in_progress", f"a land in {repo} is still running")
    if hasattr(sink, "start") and runs_as_process(action):
        command = land_command(action, deps, {})
        if not isinstance(command, tuple):
            return command
        started, argv = command
        return sink.start(started, argv)
    land = {"land": perform_land, "partial_land": perform_partial_phase}.get(action.get("kind", ""), perform_land_phase)
    return sink.submit(action, lambda: land(action, deps, {}))


def _swept_branches(deps: Deps, repo: str, pattern: str, carry: set[str]) -> tuple[list[str], str]:
    """Like `deps.delete_branches`, but a branch on a carried phase is named and kept, not deleted.

    `deps.delete_branches` force-deletes every branch under `pattern` with no way to exempt one, so a carried
    sweep lists the matches itself and deletes only those not on a carried phase.
    """
    if not carry:
        return deps.delete_branches(repo, pattern)
    code, listing = deps.run(["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}"])
    if code != 0:
        return [], listing or f"git for-each-ref exited {code}"
    prefix = pattern.removesuffix("*")
    names = [n for n in listing.split() if n.startswith(prefix) and n.rsplit("/", 1)[-1] not in carry]
    outcomes = [(name, deps.run(["git", "-C", repo, "branch", "-D", name])) for name in names]
    deleted = [name for name, (c, _) in outcomes if c == 0]
    return deleted, "".join(out for _, (c, out) in outcomes if c != 0)


def over_ssh(deps: Deps, ssh: str) -> Deps:
    """`deps` with its git door, and the sweep built on it, running on the host at `ssh` instead of here."""
    def run(argv: list[str]) -> tuple[int, str]:
        return deps.run(ssh_argv(ssh, argv))

    return replace(deps, run=run, delete_branches=partial(delete_branches_with, run))


def _host_repo(deps: Deps, repo: str) -> str:
    """The home-relative form of `repo` for a command run on a lane host; `repo` itself when no home is known."""
    return repo_path.host_form(repo, deps.home) if deps.home else repo


def local_repo_path(repo: str, home: str = "") -> str:
    """`repo` as this machine names it, a leading tilde expanded; for argv that runs here, never on a lane host."""
    return repo_path.expand(repo, home or os.path.expanduser("~"))


def _live_runs_line(runs: Sequence[Mapping[str, Any]]) -> str:
    """The refusal line naming each live run with its pid and state; "" when `runs` is empty."""
    named = "; ".join(f"{r['run']} pid {r['pid']} {r['state']}" for r in runs)
    return f"clear refused, a run of this initiative is live: {named}" if runs else ""


def _pruned(
    deps: Deps, repo: str, initiative: str, pattern: str, carry: set[str], git_repo: str = ""
) -> tuple[list[str], str]:
    """The clear's worktree prune, then its branch sweep, through `deps.run`; (deleted branches, failure line or "").

    `git_repo` is the path git runs in when it differs from `repo`, the name the lines carry; "" means `repo`.
    """
    where = git_repo or repo
    # Local pidfiles only, for the host's prune too: a process on another machine is covered by its unexpired lease.
    refused = _live_runs_line(epic.live_runs(deps.runs_dir, initiative))
    if refused:
        return [], refused
    code, listing = deps.run(["git", "-C", where, "worktree", "list", "--porcelain"])
    if code != 0:
        return [], f"git worktree list in {repo}: {listing.strip()}"
    entries = chair_plan_prune.worktrees_to_prune(listing, initiative)
    for argv in chair_plan_prune.prune_argv(entries, carry):
        code, output = deps.run(["git", "-C", where, *argv[1:]])
        if code != 0:
            return [], f"{' '.join(argv)} in {repo}: {output.strip()}"
    # The prune already deleted its entries' branches, so the sweep finding nothing after a prune is success, not a miss.
    swept, error = _swept_branches(deps, where, pattern, carry)
    deleted = [e["branch"] for e in entries if e["branch"].rsplit("/", 1)[-1] not in carry] + swept
    return deleted, f"deleted {deleted} in {repo}; git: {error.strip()}" if error else ""


def _branch_on_host(
    deps: Deps, at_home: Run, ssh: str, repo: str, branch: str, host_repo: str = ""
) -> tuple[bool, str]:
    """(present on the host, failure line or ""). A branch only the chair holds is pushed to the host first, so
    the relaunch there builds on it; a branch on neither machine is nothing to carry.

    `host_repo` is `repo` as the host names it; "" means `repo`. Lines keep naming `repo`."""
    ref = f"refs/heads/{branch}"
    here = local_repo_path(repo, getattr(deps, "home", ""))  # `deps.run` is local; only `at_home` reaches the host
    verify = ["git", "-C", here, "rev-parse", "--verify", "--quiet", ref]
    on_host, host_out = at_home(["git", "-C", host_repo or repo, "rev-parse", "--verify", "--quiet", ref])
    if on_host == 124:
        return False, f"git rev-parse of {branch} on {ssh}: {host_out.strip()}"
    if on_host == 0:
        return True, ""
    if deps.run(verify)[0] != 0:
        return False, ""
    code, out = deps.run(["git", "-C", here, "push", f"{ssh}:{host_repo or repo}", f"refs/heads/{branch}:refs/heads/{branch}"])
    return (True, "") if code == 0 else (False, f"git push of {branch} to {ssh}:{repo}: {out.strip()}")


def _carry_forward(
    action: Action, deps: Deps, repo: str, initiative: str, phases: list[str], ssh: str = ""
) -> tuple[list[str], Action | None, Result | None]:
    """Merge main into each carried phase's branch, one throwaway worktree at a time; the repo the running loop
    imports from is never checked out or switched.

    A non-empty `ssh` carries on the action's `host` instead, after syncing that host's main, because the
    relaunch runs there and reads that host's copy of the branch. A branch on neither machine is skipped.

    Returns the phases actually merged, an extra needs_chair action for the first conflict (or None), and a
    failed Result that stops the clear immediately (or None). At most one of the last two is ever set: a
    conflict stops the loop without failing the clear, any other failed git step fails it.
    """
    host = action.get("host", "")
    at_home = over_ssh(deps, ssh).run if ssh else deps.run
    # How `at_home` names the repo: the home-relative form on a host, the expanded path here; lines keep `repo`.
    # `getattr`: a caller may pass a bare stub with no `home`, and the expansion then uses this machine's HOME.
    home_repo = _host_repo(deps, repo) if ssh else local_repo_path(repo, getattr(deps, "home", ""))
    if ssh and phases:
        # The relaunch's own launch_on_host runs this same sync before starting the lane, so a host checkout that
        # is dirty or off main would fail that relaunch anyway; failing here says why one step earlier.
        code, out = at_home(sync_argv(home_repo))
        if code != 0:
            return [], None, make_result(action, "failed", f"updating {repo} on {host} before the carry: {out.strip()}")
    merged: list[str] = []
    base = ""
    if phases:
        # Resolved in the host checkout after its fetch: `origin/<base>`, never a local branch that may be stale or absent.
        try:
            base = base_branch.base_ref(home_repo, target_branch_of(deps, initiative), base_branch.configured_base(repo), at_home)
        except (base_branch.BaseError, TargetUnreadable) as exc:
            return [], None, make_result(action, "failed", f"resolving the base to carry onto in {repo}{f' on {host}' if ssh else ''}: {exc}")
    for phase in phases:
        branch = f"epic/{initiative}/{phase}"
        present, error = _branch_on_host(deps, at_home, ssh, repo, branch, home_repo) if ssh else (True, "")
        if error:
            return merged, None, make_result(action, "failed", error)
        if not present:
            continue
        if ssh:
            code, out = at_home(["mktemp", "-d", "/tmp/cox-carry-XXXXXX"])
            tmp = out.strip().splitlines()[-1] if code == 0 and out.strip() else ""
            if not tmp:
                return merged, None, make_result(action, "failed", f"mktemp -d on {host}: {out.strip()}")
        else:
            tmp = tempfile.mkdtemp(prefix="cox-carry-")
        code, out = at_home(["git", "-C", home_repo, "worktree", "add", tmp, branch])
        if code != 0:
            return merged, None, make_result(action, "failed", f"git worktree add {tmp} {branch} in {repo}: {out.strip()}")
        # The `-C tmp` calls below name a mkdtemp path on whichever machine `at_home` runs on: already absolute.
        code, out = at_home(["git", "-C", tmp, "merge", "--no-edit", base])
        if code != 0:
            _, diff_out = at_home(["git", "-C", tmp, "diff", "--name-only", "--diff-filter=U"])
            conflicted = [line for line in diff_out.splitlines() if line.strip()]
            if not conflicted:
                at_home(["git", "-C", home_repo, "worktree", "remove", "--force", tmp])
                return merged, None, make_result(action, "failed", f"git merge --no-edit {base} in {tmp} ({branch}): {out.strip()}")
            at_home(["git", "-C", tmp, "merge", "--abort"])
            rcode, rout = at_home(["git", "-C", home_repo, "worktree", "remove", "--force", tmp])
            if rcode != 0:
                return merged, None, make_result(action, "failed", f"git worktree remove --force {tmp} in {repo}: {rout.strip()}")
            needs_chair: Action = {
                "kind": ActionKind.NEEDS_CHAIR, "initiative": initiative, "cause": "carry_conflict", "epoch": action.get("epoch", 0),
                "reason": f"phase {phase} conflicts with {base}: {conflicted}",
            }
            return merged, needs_chair, None
        rcode, rout = at_home(["git", "-C", home_repo, "worktree", "remove", "--force", tmp])
        if rcode != 0:
            return merged, None, make_result(action, "failed", f"git worktree remove --force {tmp} in {repo}: {rout.strip()}")
        merged.append(phase)
    return merged, None, None


def _with_head(action: Action, deps: Deps, repo: str) -> Action:
    """The land_phase action with `head` set to the tip of its phase branch just before the land; unchanged, with no `head` key, when the branch does not resolve."""
    initiative, phase = action.get("initiative", ""), action.get("phase", "")
    tip = deps.rev_parse(repo, f"epic/{initiative}/{phase}") if initiative and phase else None
    return {**action, "head": tip} if tip else action  # type: ignore[typeddict-item]


def perform_land_phase(action: Action, deps: Deps, blocked: dict[str, Blocker]) -> Result:
    """Same shape as `perform_land`, one command for a whole phase: its own `mark_done:` lines mark every task in the
    phase done, so this only runs the command and reads its output."""
    command = land_command(action, deps, blocked)
    if not isinstance(command, tuple):
        return command
    action, argv = command
    code, output = _run_land(deps, argv)
    # After the timeout and busy checks, before the rest: a missing phase record falls back to per-task lands.
    if code != TIMED_OUT and not _repo_busy(output) and lacks_phase_record(code, output) and action.get("task_runs"):
        return _land_by_task(action, output, deps, blocked)
    return land_outcome(action, deps, code, output)


def _already_landed_result(action: Action, deps: Deps, output: str) -> Result | None:
    """Edge. A recorded result when the land stopped at squash_phase on a missing phase branch and the store has every
    task of the phase done; else None. The store is read only for a squash_phase stop.

    The stop's detail is git's stderr and may run past its first line ("Preparing worktree" before the fatal), so
    the check reads from the stop line to the end of the output."""
    stop = land_stop(output)
    if stop is None or stop[0] != "squash_phase":
        return None
    phase = action.get("phase", "")
    whole_detail = output[output.rindex(f"land: stopped at {stop[0]}:") :]
    if not already_landed(stop[0], whole_detail, deps.phase_states(action.get("initiative", ""), phase)):
        return None
    return {**make_result(action, "recorded", f"land phase {phase} already landed: {stop[1]}"), "already_landed": True}


def _land_by_task(action: Action, refused: str, deps: Deps, blocked: dict[str, Blocker]) -> Result:
    """A land_phase refused for a missing phase record, landed by task in the same tick: its `task_runs` as a
    partial_land's task lands, siblings backed up and restored. The result keeps the land_phase action, so its row
    clears or counts toward the phase's backoff as a land_phase's would; the refusal leads the reason."""
    by_task: Action = {**action, "task_ids": list(action["task_runs"]), "quarantined": 0}
    result = perform_partial_phase(by_task, deps, blocked)
    reason = "\n".join(filter(None, [refused.strip(), "landing by task instead", result["reason"]]))
    return {**result, "action": action, "reason": reason}


def perform_partial_phase(action: Action, deps: Deps, blocked: dict[str, Blocker]) -> Result:
    """The approved tasks of a partial phase, one task land each, through `partial_land.perform_partial_land`:
    git runs through `deps.run`, and each task land is `perform_land`'s, so a refusal is classified and escalated as one."""
    repo = action.get("repo", "")
    if not repo or not action.get("task_ids"):
        return make_result(action, "refused", "partial_land needs a repo and approved task ids")
    if repo in blocked:
        return make_result(action, "skipped", f"an earlier land in {repo} ({blocked[repo].label}) was not counted")
    if not partial_land.steps(action):  # recorded, not escalated: the run is learned once it is fetched
        return make_result(action, "recorded", "no approved task of the phase has a known run yet")
    return partial_land.perform_partial_land(action, deps.run, lambda land: perform_land(land, deps, blocked))  # type: ignore[return-value]


def _land_blocks_clear(landing: list[dict[str, str]], repo: str, initiative: str) -> bool:
    """A pending land in `repo` blocks the clear unless every such land names another initiative.

    Branches live under epic/<initiative>/*, disjoint per initiative. A land with no initiative, or none listed
    for a repo that reports pending, is unknown and blocks.
    """
    initiatives = [land.get("initiative", "") for land in landing if land.get("repo", "") == repo]
    return not initiatives or any(i in ("", initiative) for i in initiatives)


def perform_clear(action: Action, deps: Deps, blocked: dict[str, Blocker]) -> Result:
    initiative = action.get("initiative", "")
    pattern = branch_pattern(initiative)
    repo = deps.repo_for(action)
    if pattern is None:
        return make_result(action, "refused", f"branch glob for initiative {initiative!r} is not epic/<initiative>/*")
    if not repo:
        return make_result(action, "refused", f"no repository resolved for initiative {initiative}")
    if repo in blocked:
        return make_result(action, "skipped", f"an earlier land in {repo} ({blocked[repo].label}) was not counted")
    if deps.lands is not None and deps.lands.pending(repo) and _land_blocks_clear(deps.lands.landing(), repo, initiative):
        return make_result(action, "skipped", f"a land in {repo} is still running")
    host = action.get("host", "")
    ssh = deps.ssh_for(host) if host else ""
    if host and not ssh:
        return make_result(action, "failed", f"no ssh destination for host {host}: its clear cannot run there")
    carry = set(action.get("carry", []))
    # Expanded here, at the local call, because `_pruned` and `_swept_branches` also run over ssh on a host-form path.
    deleted, error = _pruned(deps, repo, initiative, pattern, carry, local_repo_path(repo, deps.home))
    if error:
        return make_result(action, "failed", error)
    # A host-homed initiative's earlier runs left their worktrees and branches in the host's checkout, and git
    # refuses to add a worktree on a branch another worktree holds, so the host gets the same prune first.
    host_pruned = _pruned(over_ssh(deps, ssh), repo, initiative, pattern, carry, _host_repo(deps, repo)) if ssh else None
    host_deleted, error = host_pruned or ([], "")
    if error:
        return make_result(action, "failed", f"on {host}: {error}")
    merged, needs_chair, failure = _carry_forward(action, deps, repo, initiative, sorted(carry), ssh)
    if failure is not None:
        return failure
    where = f" on {host}" if host else ""
    parts = [
        p
        for p in (
            f"deleted {deleted} in {repo}" if deleted else "",
            f"deleted {host_deleted} in {repo}{where}" if host_deleted else "",
            f"carried {merged} past main{where}" if merged else "",
        )
        if p
    ]
    # Nothing stale or partial is the state a clear exists to reach: a relaunch that failed after an earlier clear
    # must not be skipped forever because that clear already deleted the branches and carried the phases forward.
    result = make_result(action, "done", "; ".join(parts) if parts else f"nothing to clear: no branch matched {pattern} in {repo}")
    return {**result, "needs_chair": needs_chair} if needs_chair is not None else result


def perform_lease(action: Action, deps: Deps) -> Result:
    holder, host = action.get("holder", ""), action.get("host", "")
    reason = action.get("reason", "")
    if reason.startswith("takeback from "):
        line = deps.acquire_lease(holder, host, steal=True, takeback=True)  # a session still holds it: the guard does not apply
    elif reason.startswith("takeover expired"):
        line = deps.acquire_lease(holder, host, steal=True)
    else:
        line = deps.acquire_lease(holder, host)
    return make_result(action, "refused", line) if line else make_result(action, "done")


def _rendered_value(value: object) -> object:
    """A list renders as the flat-list literal `_frontmatter` writes and `parse_frontmatter` reads back; anything else is unchanged."""
    if isinstance(value, list):
        return route._Raw("[" + ", ".join(route.yaml_scalar(v) for v in value) + "]")
    return value


def _initiative_tickets(work_dir: Path, initiative: str) -> list[dict]:
    """Edge. Every ticket under `work/<initiative>/*/*.md`, `work_item`'s fields plus `body` for `merge_same_phase`.

    A missing initiative directory reads as no tickets, not an error.
    """
    tickets = []
    for task_path in sorted((work_dir / "work" / initiative).glob("*/*.md")):
        if task_path.name == "initiative.md":
            continue
        try:
            text = task_path.read_text(encoding="utf-8")
        except OSError:
            continue
        fields, body = route.parse_frontmatter(text)
        item = route.work_item(fields, initiative=initiative, phase_dir=task_path.parent.name, stem=task_path.stem)
        tickets.append({**item, "body": body})
    return tickets


def ticket_paths_of(work_dir: Path, initiative: str) -> dict[str, Path]:
    """Edge. Every ticket path under `work/<initiative>/*/*.md`, keyed by its task id (file stem); `initiative.md` excluded."""
    return {
        path.stem: path
        for path in sorted((work_dir / "work" / initiative).glob("*/*.md"))
        if path.name != "initiative.md"
    }


def _write_ticket(work_dir: Path, item: dict) -> None:
    """Edge. `item` re-rendered to frontmatter and written back to `work_dir/work/<initiative>/<file>`, the shape `_initiative_tickets` read it from."""
    fields = [(key, _rendered_value(value)) for key, value in item.items() if key not in ("initiative", "file", "body")]
    text = route._frontmatter(fields, item.get("body", ""))
    (work_dir / "work" / item["initiative"] / item["file"]).write_text(text, encoding="utf-8")


def _merge_and_write(work_dir: Path, runs_dir: Path, initiative: str) -> str:
    """Edge. Merges the tickets in the store, then the files; "" on success, else the failure line, with no file written."""
    tickets = _initiative_tickets(work_dir, initiative)
    if not tickets:
        return ""
    merged_items, merges = route.merge_same_phase(tickets)
    if not merges:
        return ""
    if Path(runs_dir) == Path("."):
        return f"merge of {initiative} needs a runs directory: Deps.runs_dir is unset; no ticket file written"
    stored_heads = [r for r in run_store.read_queue(runs_dir, initiative, kind="initiative") if r.get("task_id") == initiative]
    if not stored_heads:
        return f"merge of {initiative} refused: the store has no initiative row; no ticket file written"
    seen_events = (stored_heads[0].get("extra") or {}).get("events", [])
    by_id = {item["id"]: item for item in merged_items}
    changed = [by_id[ticket_id] for merge in merges for ticket_id in merge["members"] if ticket_id in by_id]
    paths = [work_dir / "work" / initiative / item["file"] for item in changed]
    # merge_rows reads `title` off every member; a ticket file without one falls back to its id.
    titled = [{**item, "title": item.get("title", item["id"])} for item in merged_items]
    rows, events = route.merge_rows(titled, merges, initiative)
    stored = {r["task_id"]: r for r in run_store.read_queue(runs_dir, initiative, kind="task")}
    # The row each task had before the merge: the stored one, else the one its ticket file describes.
    before = {
        t["id"]: stored.get(t["id"]) or route._merge_task_row(initiative, {**t, "title": t.get("title", t["id"])}, {})
        for t in tickets
    }
    accepted = route.write_merge_rows(runs_dir, rows, events, initiative, paths)
    refused = [row["task_id"] for row in rows if row["task_id"] not in accepted]
    if not refused:
        # write_merge_rows returns task ids only; a refused initiative row shows as an event count that did not grow.
        heads = run_store.read_queue(runs_dir, initiative, kind="initiative")
        head = next((r for r in heads if r.get("task_id") == initiative), None)
        recorded = ((head or {}).get("extra") or {}).get("events", [])
        refused = [] if len(recorded) >= len(seen_events) + len(events) else [initiative]
    if refused:
        # Rows upserted before the refusal would leave a merged survivor in the store beside unmerged files.
        failed = [f"{task}: {detail}" for task in accepted if (detail := run_store.upsert_row_detail(runs_dir, before[task]))]
        outcome = f"restore refused for {'; '.join(failed)}, so the store and the workspace now disagree" if failed else "store rows restored"
        return f"merge of {initiative} refused by the store ({', '.join(refused)}); no ticket file written; {outcome}"
    for item in changed:
        _write_ticket(work_dir, item)
    # write_merge_rows recorded each file's pre-merge text; record the merged text so the export never holds it.
    queue_export.record_written_paths(Path(runs_dir).parent, paths)
    return ""


def perform_launch(action: Action, deps: Deps) -> Result:
    initiative_id = ""
    if action.get("kind") == "launch_decompose" and action.get("intake_ids"):
        idea = action["intake_ids"][0]
        chosen_id = decompose_id(idea, deps.intake_id(idea))
        if isinstance(chosen_id, Refusal):
            return make_result(action, "refused", chosen_id.reason)
        initiative_id = route_guard.fresh_initiative_id(chosen_id, deps.initiative_rows())
    if action.get("kind") in ("launch_epic", "relaunch") and action.get("initiative"):
        failure = _merge_and_write(deps.work_dir, deps.runs_dir, action["initiative"])
        if failure:
            return make_result(action, "failed", failure)
    argv = argv_for(action, initiative_id, deps.ids_mode, deps.label)
    if argv is None:
        return make_result(action, "refused", f"{action.get('kind')} names no initiative, task or intake id")
    code, output = deps.run(argv)
    if code == 0:
        return make_result(action, "done", output)
    if chair_lease_refusal.parse_lease_refusal(output) is not None:  # the previous run's lease has not expired: not a broken launch
        return make_result(action, chair_lease_refusal.STATUS, output)
    missing = idea_miss_path(output) if action.get("kind") == "launch_decompose" and action.get("intake_ids") else None
    if missing is not None:
        # The recorded row carries the miss, which `chair_facts.decompose_miss_facts` counts into the planner's backoff.
        fingerprint = deps.intake_fingerprint(action["intake_ids"][0])
        missed: Action = {**action, "idea_missing": missing}
        if fingerprint is not None:
            missed["fingerprint"] = fingerprint
        return make_result(missed, "failed", output)
    return make_result(action, "failed", output)


IDEA_MISS_PREFIX = "routing: no idea file at "  # the line `cox route launch decompose` prints when --idea names no file


def idea_miss_path(output: str) -> str | None:
    """Pure. The path a decompose launch's `routing: no idea file at <path>` line names; None when it printed none."""
    return next((line[len(IDEA_MISS_PREFIX):].strip() for line in output.splitlines() if line.startswith(IDEA_MISS_PREFIX)), None)


def _fetched_host(runs_dir: Path, run: str) -> str:
    """Edge. The host recorded in `<run>.remote.json`; "" when the file is missing or unparseable."""
    try:
        text = remote_lane.remote_record_path(runs_dir, run).read_text(encoding="utf-8")
    except OSError:
        return ""
    record = remote_lane.parse_remote_record(text)
    return record["host"] if record else ""


def fetch_status(code: int, message: str) -> Status:
    """`skipped` for a nonzero fetch that refused because the run is still live, `failed` for any other nonzero fetch."""
    live = message.startswith("fetch: refuse:") and "is still live on" in message
    return "skipped" if code != 0 and live else "failed"


def perform_fetch_exit(action: Action, deps: Deps) -> Result:
    """`cox runs fetch` for a remote run, then fold its approvals into local ticket state on success.

    A nonzero exit applies nothing and reports failed, exactly as `fetch` does; the fetch_exit stays
    unresolved for the next tick to retry, since it never landed.
    """
    run, initiative = action.get("run", ""), action.get("initiative", "")
    argv = argv_for(action)
    if argv is None:
        return make_result(action, "refused", "fetch_exit needs a run id")
    code, output = deps.run(argv)
    if code != 0:
        return make_result(action, fetch_status(code, output), output)
    approved, warnings = chair_apply_fetch.apply_fetched_approvals(deps.runs_dir, deps.work_dir, run, initiative)
    _ack_run_exited(run, deps)
    reason = "; ".join([f"fetched {run}; approved: {', '.join(approved)}", *warnings, *fetch_divergences(output)])
    return {"action": action, "status": "done", "reason": reason, "run": run, "host": _fetched_host(deps.runs_dir, run)}


def fetch_divergences(output: str) -> list[str]:
    """Pure. The `fetch: diverged:` lines of a `cox runs fetch` output, each naming the kept branch and the side ref."""
    return [line.strip() for line in output.splitlines() if "fetch: diverged:" in line]


def perform_widen_ticket(action: Action, deps: Deps) -> Result:
    """In-process, with no argv; a widened ticket's `ready` is mirrored to the store so file and store stay one record."""
    ok, reason = chair_apply_widen.apply_widen(deps.work_dir, action, deps.now()[:10])
    if not ok:
        return make_result(action, "failed", reason)
    warning = store_cli.mirror_state(deps.runs_dir, action.get("initiative", ""), action.get("task_id", ""), "ready", "chair")
    return make_result(action, "done", reason if warning is None else f"{reason}; {warning}")


def run_exited_ids(entries: list[dict], run: str) -> list[str]:
    """Ids of the unacked `run_exited` entries that announce `run`, in first-seen order."""
    return [
        e["id"] for e in entries
        if not e.get("ack") and (parsed := courier.parse_run_exited(e)) is not None and parsed[0] == run
    ]


def _ack_run_exited(run: str, deps: Deps) -> list[str]:
    """Edge. Acks, through `courier.ack_file`, every open `run_exited` entry for `run` in `work_dir/courier.jsonl`;
    returns the ids acked. A missing or unreadable file, or a failed ack, leaves the fetch result as it was."""
    path = deps.work_dir / "courier.jsonl"
    try:
        entries = courier.entries(courier.read_text(path))
    except (OSError, ValueError):
        return []
    acked = []
    for message_id in run_exited_ids(entries, run):
        try:
            if courier.ack_file(path, message_id):
                acked.append(message_id)
        except OSError:
            continue
    return acked


_LAND_STOP_RE = re.compile(r"^land: stopped at (\S+): (.*)$", re.MULTILINE)


def land_stop(output: str) -> tuple[str, str] | None:
    """The `(step, detail)` of the last `land: stopped at <step>: <detail>` line in a land's output, else None."""
    stops = _LAND_STOP_RE.findall(output)
    return (stops[-1][0], stops[-1][1].strip()) if stops else None


_MISSING_BRANCH = "invalid reference"  # git's fatal for a ref that does not exist, here the deleted epic/<initiative>/<phase>


def already_landed(step: str, detail: str, task_states: Sequence[str]) -> bool:
    """True when a land stopped at squash_phase on a missing phase branch and every task of the phase is done.

    An empty `task_states` is unknown, not done, so it is never already landed."""
    return step == "squash_phase" and _MISSING_BRANCH in detail and bool(task_states) and all(t == "done" for t in task_states)


def _text(captured: str | bytes | None) -> str:
    """`TimeoutExpired` hands back bytes or str, or None when nothing was printed."""
    return captured.decode(errors="replace") if isinstance(captured, bytes) else captured or ""


@dataclass(frozen=True)
class ArgvResult:
    """One finished argv. `stdout` and `stderr` are the child's own streams; `mixed` is what `run_argv` has always
    returned: stdout then stderr, with the 127/124 message ahead of them. A caller that parses stdout reads `stdout`."""

    returncode: int
    stdout: str
    stderr: str
    mixed: str


def run_argv_split(argv: list[str], cwd: Path | None = None, timeout: float = LOCAL_ARGV_TIMEOUT_S,
                   env: Mapping[str, str] | None = None) -> ArgvResult:
    """Edge. A missing binary is exit 127 and a timeout exit 124, each with its message, never an exception out of perform.

    A timeout keeps what the child printed before it was killed, after the message, so a later task can read it.
    `env` is the child's whole environment; None inherits this process's."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd, timeout=timeout, env=env)
    except OSError as error:
        message = f"{argv[0] if argv else '<empty argv>'}: {error}"
        return ArgvResult(127, "", message, message)
    except subprocess.TimeoutExpired as error:
        message = f"{argv[0] if argv else '<empty argv>'}: timed out after {timeout}s\n"
        out, err = _text(error.stdout), _text(error.stderr)
        return ArgvResult(124, out, err, message + out + err)
    return ArgvResult(done.returncode, done.stdout, done.stderr, done.stdout + done.stderr)


def launch_verdict(stdout: str) -> str:
    """The verdict of a failed `cox route launch`: its last "routing:" stdout line, with the lines after it joined on;
    else its last non-empty stdout line; "" for empty stdout.

    Launch prints each refusal last and then returns. So the last "routing:" line is the refusal, and the "routing:"
    lines before it (usage hold, go_degraded advisory, usage stop overridden by --force, store unavailable) are notes
    it printed and carried on past, as are "export:", "merged:" and "override:" lines. The lines after the refusal are
    its own detail (the dirty repo's porcelain entries, the configured lane hosts), so they stay on the verdict line.
    The leader guard's "refusing:" line comes before any "routing:" note, so the last line finds it."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    starts = [index for index, line in enumerate(lines) if line.startswith("routing:")]
    if not starts:
        return lines[-1] if lines else ""
    head, *detail = lines[starts[-1]:]
    return f"{head} {'; '.join(detail)}" if detail else head


def run_argv(argv: list[str], cwd: Path | None = None, timeout: float = LOCAL_ARGV_TIMEOUT_S,
             env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Edge. The exit code and the mixed stdout-then-stderr text of `run_argv_split`.

    A failed `cox runs fetch` or `cox route launch` is the exception: git's long stderr, or held-export warnings,
    would push cox's own stdout verdict ("fetch: verify: ...", "routing: ...") out of the 600-character reason, so its
    text is `failed_reason`, verdict first. Fetch prints its verdict first; launch's is picked by `launch_verdict`.
    Other argvs keep the mixed text because `land_refusal` reads git's stderr ("could not apply") out of it."""
    done = run_argv_split(argv, cwd, timeout=timeout, env=env)
    if done.returncode in (0, 124, 127):
        return done.returncode, done.mixed
    if argv[:3] == ["cox", "runs", "fetch"]:
        return done.returncode, failed_reason(done.stdout, done.stderr)
    if argv[:3] == ["cox", "route", "launch"]:
        return done.returncode, failed_reason(launch_verdict(done.stdout), done.stderr)
    return done.returncode, done.mixed


def land_commit(repo_dir: str) -> str:
    """Edge. `origin/main`'s commit hash in `repo_dir`, read right after a true `landed()` result so it reflects
    the land that just happened before anything else can move origin/main; "" on any git failure."""
    code, output = run_argv(repo_path.git_argv(repo_dir, os.path.expanduser("~"), "log", "-1", "--format=%H", "origin/main"))
    return output.strip() if code == 0 else ""


def delete_branches_with(run: Run, repo: str, pattern: str) -> tuple[list[str], str]:
    """Force-delete local branches under `pattern` in `repo`; (deleted names, error text)."""
    code, listing = run(["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}"])
    if code != 0:
        return [], listing or f"git for-each-ref exited {code}"
    prefix = pattern.removesuffix("*")
    outcomes = [(name, run(["git", "-C", repo, "branch", "-D", name])) for name in listing.split() if name.startswith(prefix)]
    deleted = [name for name, (c, _) in outcomes if c == 0]
    return deleted, "".join(out for _, (c, out) in outcomes if c != 0)
