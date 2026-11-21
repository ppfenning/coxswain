"""The epic driver's run context: `Ctx`, the store connection it retries, the pool setting and the phase order."""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from operator import attrgetter
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from core.cartridge import integration_checks_setting
from core.jsontypes import is_list, is_mapping
from core.landing import pr_branch_name
from graphs._spec import GraphSpec
from harness import pool_edge
from harness.artifacts import ArtifactsRoot
from harness.check_gate import gate_checks
from harness.checks import check_timeout
from harness.epic_types import PhaseRecord
from harness.store_dialect import Connection, connect
from harness.store_retry import retry_store_write
from harness.store_write import Store


def git_in(*args: str, cwd: Path | None = None) -> tuple[bool, str]:
    """Run one git command and report what happened, never what was intended."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, f"git {' '.join(args)} timed out after 120s"
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


class _ConninfoOption(Protocol):
    """One entry of libpq's `pgconn.info`: the part `_reopen_url` reads."""

    keyword: bytes
    val: bytes | None


def _reopen_url(conn: Connection) -> str | None:
    """A URL `connect` accepts for the database `conn` is on, or None when it cannot be reopened (an in-memory sqlite)."""
    raw = conn.raw
    if isinstance(raw, sqlite3.Connection):
        file = next((str(row[2]) for row in raw.execute("PRAGMA database_list") if row[1] == "main"), "")
        return f"sqlite:///{file}" if file else None
    # psycopg's `info.get_parameters()` never returns the password; the libpq `pgconn.info` does.
    info: Iterable[_ConninfoOption] = attrgetter("pgconn.info")(raw)  # a psycopg connection: RawConnection does not name it
    params = {item.keyword.decode(): item.val.decode() for item in info if item.val}
    return "postgresql:///?" + urlencode(params)


def reopen_connection(conn: Connection) -> None:
    """Replace the driver connection under `conn` with a fresh one to the same database.

    Every holder of `conn` (the heartbeat, the runner) sees the new connection. The old one is closed only
    after the new one is open, so a server still down leaves `conn` as it was and raises for the retry to repeat.
    """
    url = _reopen_url(conn)
    if url is None:
        return
    fresh = connect(url)
    old, conn.raw = conn.raw, fresh.raw
    with contextlib.suppress(Exception):  # the old connection is already dead
        old.close()


@dataclass(frozen=True)
class StoreRetry:
    """How a store write is retried across a lost connection. The default reconnects nothing: see `for_store`."""

    reconnect: Callable[[], None] = lambda: None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def for_store(
        cls, store: Store, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep
    ) -> StoreRetry:
        """Retries that reconnect `store`'s own connection."""
        return cls(reconnect=lambda: reopen_connection(store.conn), clock=clock, sleep=sleep)

    def write[T](self, call: Callable[[], T], window_s: float) -> T:
        """Run `call`, reconnecting and retrying a lost connection for `window_s`; any other error, or the window's end, raises."""
        return retry_store_write(
            call, reconnect=self.reconnect, clock=self.clock, sleep=self.sleep, window_s=window_s
        )


@dataclass(frozen=True)
class PoolSetting:
    """Warm pool of task worktrees on this host; `run_epic` leaves it off unless one is passed.

    `keep` is the most idle directories left after the run. The pool's capacity is `pool_size(max_parallel)`.
    The clock is wall time because the pool index outlives the process.
    """

    pool_root: Path
    sync_argv: Sequence[str]
    lock_names: Sequence[str]
    keep: int
    clock: Callable[[], float] = time.time


@dataclass(frozen=True)
class Ctx:
    """Everything the per-phase work needs, fixed for the whole run.

    A frozen record rather than a dozen parameters threaded through six
    functions: the phase loop is where the interesting decisions are, and they
    read better when the configuration is not standing in front of them.
    """

    repo: Path
    cartridge: Mapping[str, object]
    runner: Any  # a NodeRunner, or a view on one; read for the attributes a test runner adds (`calls`, `verify_by_task`)
    specs: Mapping[str, GraphSpec]
    run_id: str
    date: str
    max_parallel: int
    ledger_path: Path
    provider_profile: str
    runs_dir: Path
    worktree_root: Path
    assume: str | None
    fix_attempts: int | None
    initiative_id: str
    default_ref: str
    land_at: str = "initiative"
    # The initiative's one branch, from its landing record: set once the run has started the record. Every phase is
    # cut from it, each phase merges back into it when built, and its tip is what the one pull request opens from.
    initiative_branch: str | None = None
    # Why the initiative branch does not hold its parent's current tip, when a run that needs it asked and could
    # not have it (a conflict, a held lease). None when it holds it, or when nothing in the initiative needs it.
    parent_behind: str | None = None
    # The run's runner before any phase took its view of it. A rebuild from a last attempt takes a view on that
    # attempt's tree, and a view is never stacked on another phase's view.
    shared_runner: Any = None
    resume_from: str | None = None
    repo_checks: Sequence[Mapping[str, object]] = field(default_factory=list[Mapping[str, object]])
    store: Store | None = None
    epoch: int | None = None
    lease_name: str | None = None
    work_state: str = "files"
    retry: StoreRetry = field(default_factory=StoreRetry)
    # `landing_areas.test_database`, read once: each task lane runs its checks against its own database.
    test_database: Mapping[str, str] | None = None
    # Workers each lane's checks may use; None when no lane capacity was stated.
    lane_workers: int | None = None
    # Run-level and mutable, shared by `replace`: lanes holding a database, and the drop records (the cleanup list).
    lane_names: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str], compare=False, repr=False)
    lane_drops: list[dict[str, object]] = field(default_factory=list[dict[str, object]], compare=False, repr=False)
    pool: PoolSetting | None = None
    # The provider profile's `artifacts_url`, resolved once; None leaves every step's artifacts in the run folder only.
    artifacts: ArtifactsRoot | None = None
    # Run-level and mutable: the lease each pooled task build holds, with the lock digest read when it was acquired.
    pool_leases: dict[tuple[str, str], tuple[pool_edge.Lease, str]] = field(
        default_factory=dict[tuple[str, str], tuple[pool_edge.Lease, str]], compare=False, repr=False
    )
    # The one mutable member of a frozen record: file listings by commit (see `default_branch_paths`).
    surface_paths: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]], compare=False, repr=False)
    # Guards writes to the shared `items` list and the lazy fill of `surface_paths`; `replace` shares it across per-phase copies.
    items_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)
    # Run-level and mutable: the future of each phase this run launched, so a dependent that starts early can wait for the cut.
    # The scheduler thread sets one key per launch and workers only `get`, each atomic, and a parent is set before any dependent starts.
    launched: dict[str, Future[PhaseRecord]] = field(default_factory=dict[str, Future[PhaseRecord]], compare=False, repr=False)
    # Run-level and mutable: the repositories whose phase branch fetch failed while checking merged commits, faulted once.
    merge_fetch_faults: set[str] = field(default_factory=set[str], compare=False, repr=False)

    def __post_init__(self) -> None:
        """Refuse a bad per-check timeout at build, before any model call; the ValueError names the check."""
        for check in self.checks:
            check_timeout(check, 600)

    # ── names, in one place, so the topology is readable ─────────────────────
    def phase_branch(self, phase: str) -> str:
        return f"epic/{self.initiative_id}/{phase}"

    def pr_branch(self) -> str:
        return self.initiative_branch or pr_branch_name(self.initiative_id)

    def pr_branch_ref(self) -> str | None:
        """The pr branch as a ref git can cut from: the local branch, else its copy on origin, else None."""
        name = self.pr_branch()
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            if git_in("-C", str(self.repo), "rev-parse", "--verify", "--quiet", ref)[0]:
                return ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
        return None

    def draft_branch(self, phase: str, task: str) -> str:
        # NOT `epic/<initiative>/<phase>/<task>`: git cannot hold both
        # `refs/heads/epic/i/p1` and `refs/heads/epic/i/p1/t1`, because one is a
        # file where the other needs a directory. The draft namespace is
        # therefore flattened with `--` under the phase, which keeps
        # `git branch --list 'epic/<initiative>/*'` reading as the stack it is.
        return f"epic/{self.initiative_id}/{phase}--{task}"

    def scratch_branch(self, task: str) -> str:
        # Harness-owned, per run, never promoted: this is the namespace the
        # contract's worktree exception covers. The draft branch above is
        # created only after the gate.
        return f"agents/{self.run_id}/{task}"

    def phase_worktree(self, phase: str) -> Path:
        return self.worktree_root / self.run_id / phase

    def task_worktree(self, phase: str, task: str) -> Path:
        held = self.pool_leases.get((phase, task))
        return held[0].path if held else self.phase_worktree(phase) / task

    @property
    def landing_areas(self) -> Mapping[str, object]:
        """The cartridge's `landing_areas`; empty when it has none."""
        areas = self.cartridge.get("landing_areas")
        return areas if is_mapping(areas) else {}

    @property
    def checks(self) -> list[Mapping[str, object]]:
        listed = self.landing_areas.get("checks")
        cartridge_checks = [c for c in listed if is_mapping(c)] if is_list(listed) else []
        known = {c.get("cmd") for c in cartridge_checks}
        extra = [c for c in self.repo_checks if c.get("cmd") not in known]
        return cartridge_checks + extra

    @property
    def level_checks(self) -> list[Mapping[str, object]]:
        """The cartridge's level gate; never joined with `.agent-checks`, and never read by a task build."""
        return list(gate_checks(self.landing_areas, "level"))

    @property
    def integration_checks(self) -> list[Mapping[str, object]]:
        """The cartridge's touched-package integration checks; empty when none are configured."""
        return [c for c in integration_checks_setting(self.cartridge) if is_mapping(c)]

    @property
    def bound(self) -> Mapping[str, object]:
        skills = self.cartridge.get("skills")
        return skills if is_mapping(skills) else {}


# ── the phase graph ─────────────────────────────────────────────────────────


def phase_order(parents: Mapping[str, set[str]]) -> tuple[list[str], list[str]]:
    """Phases in dependency order with ties broken by name, then the unorderable.

    Ties break by name so two runs over one initiative walk the phases in the
    same order — the same reason `invoke_graphs` reads its results back sorted.
    Whatever is left over sits in a cycle BETWEEN phases: a task DAG can be
    acyclic while the phase graph it induces is not, and a cycle is reported
    rather than resolved.
    """
    remaining = {phase: set(deps) for phase, deps in parents.items()}
    ordered: list[str] = []
    while remaining:
        free = sorted(phase for phase, deps in remaining.items() if not deps - set(ordered))
        if not free:
            break
        for phase in free:
            ordered.append(phase)
            del remaining[phase]
    return ordered, sorted(remaining)


def _phase_entries(initiative: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The initiative's `phases` entries that are objects (`{id, goal}`); a bare phase name is none."""
    phases = initiative.get("phases")
    return [p for p in phases if is_mapping(p)] if is_list(phases) else []


def declared_phases(initiative: Mapping[str, object], items: Sequence[Mapping[str, object]]) -> list[str]:
    """Phase ids in declared order; the sorted phase names on the items when no entry carries an id."""
    declared = [str(p["id"]) for p in _phase_entries(initiative) if p.get("id")]
    return declared or sorted({str(i["phase"]) for i in items if i.get("phase")})


def unreached_warning(unreached: Mapping[str, Mapping[str, Sequence[str]]]) -> str | None:
    """One warning line naming the phases that build without earlier phases they share files with, else None."""
    if not unreached:
        return None
    return (
        f"  warning: phase(s) {', '.join(sorted(unreached))} will build without the earlier phases listed as not "
        "inherited; a task dependency on a task in that phase would add it"
    )


_EVIDENCE_LINE_CAP = 400


def read_evidence_file(worktree: str, rel: str) -> str | None:
    """The reader `phase-validate` calls for a chunk verdict's `needs_evidence`.

    The one place this driver opens a file on the graph's behalf, injected in
    as an argument per docs/GRAPH-CONTRACT.md clause 4 — the graph itself
    never touches a filesystem. The file-count cap and the check that keeps a
    request inside `worktree` are the graph's own, over the names alone; this
    only caps line count and reports an unreadable path as `None`, never a
    raise, because one bad name must not cost the rest of the phase's verdict.
    """
    try:
        text = Path(worktree, rel).read_text()
    except OSError:
        return None
    return "\n".join(text.splitlines()[:_EVIDENCE_LINE_CAP])


def phase_goal(initiative: Mapping[str, object], phase: str) -> str:
    """The phase's ORIGINAL goal — the thing `validate_phase` judges against.

    A work store's phases are bare names (`workstore.phases` returns the set of
    directory names), so where a goal was never recorded the honest substitute
    is the phase's name plus the initiative's own prose. Not a restatement of
    the task list: that is exactly what the phase verdict must never be allowed
    to reduce to. Where a decompose-produced initiative carries `{id, goal}`
    phase entries, they win.
    """
    for entry in _phase_entries(initiative):
        if str(entry.get("id")) == phase and entry.get("goal"):
            return str(entry["goal"])
    body = str(initiative.get("body") or "").strip()
    return f"phase '{phase}' of initiative '{initiative.get('id')}'" + (f"\n\n{body}" if body else "")
