"""The command line over the harness.

Thin on purpose. Everything here is argument plumbing; the machinery it drives
lives in the sibling modules, and the graphs it offers come from discovery
rather than a dispatch table — `python shell.py <graph>` works for any module
under `graphs/` that declares a SPEC.

`phase` is the one subcommand that is not a graph: it is the harness's own
driver, running the lifecycle graph once per ready task, concurrently.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import faulthandler
import hashlib
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

import yaml

import core
from core import ledger, workstore
from core.cartridge import CartridgeError
from core.landing import resolve_land_at
from core.manifest import append_ledger, build_manifest
from graphs._contract import ContractViolation, landing_for
from graphs.delivery.initiative_decompose import GRAPH_NAME as _DECOMPOSE_GRAPH_NAME
from graphs.delivery.initiative_decompose import (
    LintRefusal,
    approved_task_ids,
    decompose_closing,
    expected_task_files,
    first_problem_line,
    phase_dirs,
    unexecuted_item_create_reason,
    withdrawal,
)
from harness import CORE_SCHEMA, node_progress, run_lease, store_traces
from harness.autonomy import split_by_policy
from harness.check_gate import gate_commands
from harness.checks import all_passed, checks_evidence, repo_checks, run_checks
from harness.digest import build_digest
from harness.escalate import escalate_self_modification
from harness.gate import apply_decisions, auto_apply, gate
from harness.lane_workers import LANE_CAPACITY_VAR, check_env, lane_workers_from
from harness.lanes import resolve_max_parallel
from harness.phase import run_phase
from harness.registry import GraphSpec, discover
from harness.resolve import overlay_path, resolve_cartridge, role_skill_bodies
from harness.runners import build_runner
from harness.store_dialect import default_url
from harness.store_graphs import derive_definition, register
from harness.store_lease import acquire, release
from harness.store_migrate import open_store
from harness.store_read import calls as node_calls
from harness.store_read import cost_by_model, run_summary
from harness.store_write import Store
from harness.termination import mark_terminating
from harness.traces_url import have_pyarrow, redact_url, resolve_traces_root
from harness.usage import summarize
from harness.worktree import apply_patch, create_worktree, keep_worktree, remove_worktree
from runner.protocol import LimitStop, RunnerError

if TYPE_CHECKING:
    from harness.epic import PoolSetting

__all__ = ["main"]

REPO_ROOT = Path(__file__).resolve().parent.parent
# The cartridges component beside this one in the coxswain checkout: src/graphs -> src/cartridges.
CARTRIDGES_ROOT = REPO_ROOT.parent / "cartridges"
DEFAULT_POOL_KEEP = 4


def _default_ledger() -> Path:
    """Where the trust record lives when nobody says otherwise: OUT of the tree.

    The obvious default is `REPO_ROOT / "ledger.jsonl"`, and it is wrong. This
    system patches its own working tree and, under the epic driver, branches it.
    Path-protection via escalation only governs changes that arrive as
    proposals — a file inside the tree can also be edited by any approved patch
    that claims some other purpose entirely, and a trust record you can reach
    through the very thing it governs is not a record. Out of the tree, no patch
    the system applies can touch it; and `governance_hits` still matches the
    ledger on BASENAME, so a patch that tries to plant a shadow copy inside the
    tree escalates instead of quietly becoming the ledger.

    Read at call time, not import time: `XDG_STATE_HOME` is environment, and
    environment is something a test — or a user — gets to change.
    """
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state).expanduser() / "agent-graphs" / "ledger.jsonl"


def _observe_trap_failures(
    result: dict[str, Any],
    *,
    graph_name: str,
    ts: str,
    cartridge: dict[str, Any],
    provider_profile: str,
    ledger_path: Path | str,
) -> int:
    """File a `failure` observation for every runbook entry whose trap did not hold.

    Verify is a detector, and detectors file observations. Today its verdict
    reaches a `doc_update` proposal and nothing else, so an entry demonstrated
    wrong IN USE keeps its streak until a human happens to refuse something —
    which is backwards: the run already established the fact, and standing
    should not wait on someone noticing. Rule 3 has always contemplated this
    shape ("a post-hoc detector fired"); this is the detector.

    Deliberately narrow:

    -   `is False` exactly. A missing or None `trap_held` is a graph that did
        not answer, not evidence the trap was wrong, and demoting on silence
        would make the detector punish incomplete runs.
    -   Unverified items observe nothing. An item deferred for capacity was
        never checked; it has no verdict to file.
    -   A subject_new gap — no runbook entry matched — observes nothing. There
        is no streak to demote, and inventing a subject for an entry that does
        not exist yet would create a track record out of its absence.

    Returns how many observations were filed. The row is a `failure` by
    construction (`append_observation` sets the outcome), which per the policy
    resets THAT ENTRY's streak and doubles its bar while every other entry's
    streak stands untouched.
    """
    triaged = result.get("triaged") or []
    hits = [
        entry
        for item in triaged
        if item.get("verified")
        and (item.get("verification") or {}).get("trap_held") is False
        and (entry := str((item.get("classification") or {}).get("runbook_entry") or "").strip())
    ]
    if not hits:
        return 0

    # Risk comes off the taxonomy, never from here. A cartridge that cannot name
    # the kind cannot say what it risks, and a row with an invented risk is a
    # row the policy would count against the wrong bar.
    spec = (cartridge.get("write_kinds") or {}).get("doc_update")
    risk = spec.get("risk") if isinstance(spec, Mapping) else None
    if not risk:
        print(
            f"trap did not hold for {len(hits)} entry(ies) but cartridge "
            f"'{cartridge.get('team', '?')}' declares no risk for 'doc_update'; "
            "no observation recorded — risk is read off the taxonomy, never invented",
            file=sys.stderr,
        )
        return 0

    for entry in hits:
        ledger.append_observation(
            {
                "run_id": result.get("run_id"),
                "ts": ts,
                "principal": graph_name,
                "kind": "doc_update",
                "risk": risk,
                "subject": entry,
                "cartridge_sha": cartridge.get("cartridge_sha"),
                "overlay_sha": cartridge.get("overlay_sha"),
                "provider_profile": provider_profile,
                "schema": core.SCHEMA_VERSION,
            },
            ledger_path,
        )
        print(f"observation: trap did not hold for '{entry}' — recorded against its streak")
    return len(hits)


def _core_schema_status(installed: str, required: str) -> tuple[str, str] | None:
    """Compare MAJOR.MINOR core schema strings; None means proceed silently.

    Otherwise ("fatal", message) on a MAJOR difference or ("warn", message)
    on a MINOR-only difference.
    """
    installed_major, installed_minor = (int(p) for p in installed.split(".", 1))
    required_major, required_minor = (int(p) for p in required.split(".", 1))
    if installed_major != required_major:
        return "fatal", f"core schema {installed} does not match harness CORE_SCHEMA {required}; update the coxswain checkout"
    if installed_minor != required_minor:
        return "warn", f"core schema {installed} does not match harness CORE_SCHEMA {required}"
    return None


def _governance_line(hits: list[str], *, label: str = "") -> str:
    where = f"{label}: " if label else ""
    return (
        f"{where}governance paths touched ({len(hits)}): "
        f"{', '.join(hits)} — proposals escalated to self_modification"
    )


_INTEGRATION_KEYS = (
    "integration_tasks_passed",
    "integration_tasks_failed",
    "integration_levels_passed",
    "integration_levels_failed",
)


def epic_summary_line(run_id: str, totals: Mapping[str, int]) -> str:
    """The end-of-run epic line; a missing total counts as 0. The integration clause prints only when an integration check ran."""
    integration = (
        f"; integration: {totals.get('integration_tasks_passed', 0)} task(s) passed, "
        f"{totals.get('integration_tasks_failed', 0)} failed, "
        f"{totals.get('integration_levels_passed', 0)} level(s) passed, "
        f"{totals.get('integration_levels_failed', 0)} failed"
        if any(totals.get(k, 0) for k in _INTEGRATION_KEYS)
        else ""
    )
    return (
        f"\nepic {run_id}: {totals.get('phases_complete', 0)} phase(s) complete, "
        f"{totals.get('phases_partial', 0)} partial, {totals.get('phases_blocked', 0)} blocked, "
        f"{totals.get('phases_awaiting_land', 0)} awaiting land, "
        f"{totals.get('tasks_quarantined', 0)} task(s) quarantined, "
        f"{totals.get('stacks_rebased', 0)} stack(s) rebased{integration}"
    )


def _build_parser(specs: dict[str, GraphSpec]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shell.py",
        description="Resolve a cartridge, run a graph, gate its proposals, record the run.",
    )
    parser.add_argument(
        "graph",
        choices=sorted([*specs, "phase", "epic", "rescue", "sweep"]),
        help=(
            "which graph to run ('phase' drives the lifecycle graph over one phase; "
            "'epic' drives a whole initiative, phase by phase, gating each one; "
            "'rescue' runs one rescue of one quarantined task and prints its JSON report; "
            "'sweep' is registered for coxswain dispatch but not yet runnable standalone)"
        ),
    )
    parser.add_argument("--team", required=True, help="team cartridge to resolve")
    parser.add_argument(
        "--cartridges-dir",
        default=CARTRIDGES_ROOT / "cartridges",
        help="where cartridge directories live",
    )
    parser.add_argument(
        "--provider-profile",
        default=CARTRIDGES_ROOT / "providers" / "anthropic-default.yaml",
    )
    parser.add_argument("--skills-root", action="append", default=[], metavar="PATH")
    parser.add_argument("--unverified-skills", action="store_true", help="skip skill checks; warns every time")

    # Every graph's declared needs become flags. Two graphs may not claim the
    # same flag with different meanings; identical re-declarations collapse.
    seen: dict[str, str] = {}
    for spec in specs.values():
        for need in spec.needs:
            if need.flag in seen:
                if seen[need.flag] != f"{need.kind}:{need.name}":
                    raise SystemExit(
                        f"graph '{spec.name}' redefines {need.flag} with a different meaning"
                    )
                continue
            seen[need.flag] = f"{need.kind}:{need.name}"
            kwargs: dict[str, Any] = {"help": f"{spec.name}: {need.help}" if need.help else None}
            if need.kind == "int":
                kwargs["type"] = int
            if need.choices:
                kwargs["choices"] = list(need.choices)
            parser.add_argument(need.flag, **{k: v for k, v in kwargs.items() if v is not None})

    parser.add_argument("--initiative", help="phase, epic, rescue: path to the work/<initiative> directory")
    parser.add_argument("--task", help="rescue: id of the quarantined task to rescue")
    parser.add_argument("--phase-name", help="phase: which phase to run (default: the first with ready work)")
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        help="phase, epic: how many tasks run at once (default: the provider profile's max_parallel, else the free lane count)",
    )
    parser.add_argument("--scripted", metavar="JSON", help="run offline against canned node responses")
    parser.add_argument("--assume", choices=["a", "e", "r"], help="answer the gate non-interactively")
    parser.add_argument("--runs-dir", default=REPO_ROOT / "runs")
    # Runs stay in the tree — they are artifacts, and an artifact is allowed to
    # be branched away with the work it describes. The ledger is not an
    # artifact; see `_default_ledger`.
    parser.add_argument("--ledger", default=_default_ledger())
    parser.add_argument("--worktree-root", help="override the cartridge's worktree_root")
    parser.add_argument(
        "--node-cap-usd",
        type=float,
        default=None,
        help=(
            "operator's per-node spend cap; the effective limit becomes "
            "min(shape ceiling, this) and a stop at the cap is error_spend_cap"
        ),
    )
    parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="on exit, move the run's worktree under <worktree_root>/_kept/<run_id> instead of deleting it",
    )
    parser.add_argument(
        "--pool-keep",
        type=_non_negative,
        default=DEFAULT_POOL_KEEP,
        help=f"epic: most idle warm worktrees left in the pool after the run (default {DEFAULT_POOL_KEEP})",
    )
    parser.add_argument(
        "--no-pool",
        action="store_true",
        help="epic: build every task in a cold worktree instead of leasing one from the warm pool",
    )
    parser.add_argument(
        "--resume-from",
        metavar="RUN_ID",
        help=(
            "epic: reuse tasks an earlier run already produced an approved patch for "
            "(saved under runs/<RUN_ID>/tasks/); everything else runs again"
        ),
    )
    parser.add_argument(
        "--phase",
        action="append",
        default=None,
        metavar="PHASE",
        help="epic: run only the named phase, repeatable; the phases' parents are taken as complete from the work store",
    )
    parser.add_argument(
        "--repo",
        help=(
            "the repository this change targets; enables the check arm: the patch is "
            "applied in a real worktree of it and the configured checks run there "
            "before the gate"
        ),
    )
    parser.add_argument(
        "--base",
        help=(
            "epic, rescue: the branch phase branches are cut from, as `origin/<branch>`; "
            "the launcher resolves it, so the harness never looks the repo's setting up itself"
        ),
    )
    parser.add_argument(
        "--workdir",
        default=REPO_ROOT,
        help=(
            "where a runner whose nodes can read the world stands: the work store root "
            "the apply arms write under (default: this repository)"
        ),
    )
    parser.add_argument("--date", default=date_type.today().isoformat())  # noqa: DTZ011 — the operator's local date is the intended default
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--result-out", default=None, help="write the graph's result as JSON to this path")
    return parser


def _provider_profile_scope(path: Path | str) -> str:
    """`stem@sha12` of the resolved profile file's bytes (triage.md §4)."""
    resolved = Path(path)
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()[:12]
    return f"{resolved.stem}@{digest}"


_PHASE_PRINCIPAL = "phase(lifecycle-propose)"
_COS_PRINCIPAL = "coxswain(dispatch)"
_LEASE_TTL = 120  # seconds; the heartbeat renews every 30


def _read_profile(path: Path | str) -> Mapping[str, Any]:
    """The provider profile's mapping; empty when unreadable, since the scripted path may name no real file."""
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, Mapping) else {}


def _parallel_cap(parser: argparse.ArgumentParser, args: argparse.Namespace, free_lanes: int) -> int:
    """The command-line cap, else the profile's max_parallel, else `free_lanes`. A bad profile value is a usage error."""
    # Not module-level: a process importing this module must not need the cartridges package to carry the new name.
    from core.profile import parse_max_parallel

    path = getattr(args, "provider_profile", None)
    from_profile = parse_max_parallel(_read_profile(path).get("max_parallel")) if path else None
    if isinstance(from_profile, str):
        parser.error(from_profile)
    return resolve_max_parallel(args.max_parallel, from_profile, free_lanes)


def _positive_number(key: str, value: object, default: float | None) -> float | str | None:
    """`value` as a float when positive, `default` when absent, else the usage-error message naming `key`."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return f"{key} must be a positive number, got {value}"
    return float(value)


def _usage_bound(
    parser: argparse.ArgumentParser, args: argparse.Namespace, runner: Any
) -> tuple[Callable[[], float | None], float]:
    """`(headroom_usd, est_task_usd)` from the profile's run_ceiling_usd and task_estimate_usd. A bad value is a usage error."""
    path = getattr(args, "provider_profile", None)
    profile = _read_profile(path) if path else {}
    ceiling = _positive_number("run_ceiling_usd", profile.get("run_ceiling_usd"), None)
    estimate = _positive_number("task_estimate_usd", profile.get("task_estimate_usd"), 6.0)
    for parsed in (ceiling, estimate):
        if isinstance(parsed, str):
            parser.error(parsed)

    def headroom_usd() -> float | None:
        if ceiling is None:
            return None
        return ceiling - summarize(list(getattr(runner, "calls", None) or []))["cost_usd"]

    return headroom_usd, estimate


def _storage_url(profile: Mapping[str, Any], runs_dir: Path | str) -> str:
    """The profile's `storage_url`; without one, the sqlite file inside the runs directory."""
    url = profile.get("storage_url")
    return url if isinstance(url, str) and url else default_url(runs_dir)


def _traces_url(profile: Mapping[str, Any], runs_dir: Path | str) -> str | None:
    """The profile's `traces_url`; None when absent, which leaves the root at `<runs_dir>/traces`."""
    url = profile.get("traces_url")
    return url if isinstance(url, str) and url else None


def _object_store(profile: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The profile's `object_store` block; None when absent or not a mapping."""
    block = profile.get("object_store")
    return block if isinstance(block, Mapping) else None


def _principal(graph: str, specs: Mapping[str, GraphSpec], *, docket: str | None) -> str:
    """The run's principal, named as the manifest names it. `epic` is the driver's own constant."""
    if graph in ("epic", "rescue"):
        from harness.epic import PRINCIPAL

        return PRINCIPAL
    if graph == "phase":
        return _PHASE_PRINCIPAL
    if graph == "cos" and not docket:
        return _COS_PRINCIPAL
    return specs[graph].graph_name if graph in specs else graph


def _register_graph(conn: Any, specs: Mapping[str, GraphSpec], graph: str, now: str) -> str | None:
    """Register the graph this run executes and return its id.

    unknown: no graph in this repository declares a loaded graph object yet. A spec that
    carries one on `.graph` is registered; one that does not leaves the run's graph_id empty.
    """
    spec = specs.get("lifecycle" if graph in ("phase", "epic", "rescue") else graph)
    loaded = getattr(spec, "graph", None)
    return None if loaded is None else register(conn, derive_definition(loaded), now)


def _begin_store_run(
    args: argparse.Namespace,
    specs: Mapping[str, GraphSpec],
    cartridge: Mapping[str, Any],
    run_id: str,
    now: str,
    host: str,
) -> Store | None:
    """Open the run-record store, register the graph and write the run row; None (after one line) if any of it fails."""
    url = _storage_url(_read_profile(args.provider_profile), args.runs_dir)
    conn = None
    try:
        if url == default_url(args.runs_dir):
            Path(args.runs_dir).mkdir(parents=True, exist_ok=True)
        conn = open_store(url, now)
        graph_id = _register_graph(conn, specs, args.graph, now)
        try:
            provider_profile = _provider_profile_scope(args.provider_profile)
        except OSError:
            provider_profile = str(args.provider_profile)
        record = {
            "run_id": run_id,
            "principal": _principal(args.graph, specs, docket=getattr(args, "docket", None)),
            "cartridge_sha": cartridge.get("cartridge_sha"),
            "cartridge_team": cartridge.get("team"),
            "overlay_sha": cartridge.get("overlay_sha"),
            "provider_profile": provider_profile,
        }
        launch = {
            "launched_by": os.environ.get("AGENT_GRAPHS_LAUNCHED_BY") or "cli",
            "at": now,
            "graph_id": graph_id,
        }
        store = Store(conn)
        store.record_run(record, launch, host=host)
    except Exception as exc:
        if conn is not None:
            conn.close()
        print(f"store: cannot use {url.split(':', 1)[0]} store: {' '.join(str(exc).split())}", file=sys.stderr)
        return None
    return store


def _refuse_start(store: Store, run_id: str, status: str, code: int) -> int:
    """Stamp a run that never started and close its store; `code` is its exit."""
    _finish_store_run(store, run_id, datetime.now(UTC).isoformat(), status)
    store.conn.close()
    return code


def _end_lease(store: Store, heartbeat: run_lease.Heartbeat | None, name: str, holder: str, epoch: int) -> None:
    """Stop renewing, then expire the lease. Warns on any fault and never raises; an unreleased lease expires.

    `heartbeat` is None when the run raised before one was built.
    """
    if heartbeat is not None:
        heartbeat.stop()
    try:
        if not release(store.conn, name, holder, epoch):
            print(f"lease: {name} was not released: it is no longer held by {holder}", file=sys.stderr)
    except Exception as exc:
        print(f"lease: could not release {name}: {' '.join(str(exc).split())}", file=sys.stderr)


def _finish_store_run(store: Store, run_id: str, ended_at: str, status: str) -> None:
    """Stamp the run's end. A store that fails here warns; it never changes the run's exit."""
    try:
        store.finish_run(run_id, ended_at, status)
    except Exception as exc:
        print(f"store: could not record the end of {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)


def _record_refusal(store: Store | None, run_id: str, refusal: LintRefusal) -> None:
    """Store the first lint problem line as the run's outcome. A failed write warns; it never changes the exit."""
    first = first_problem_line(str(refusal).splitlines())
    if store is None or first is None:
        return
    try:
        store.record_outcome(run_id, {"refused": first})
    except Exception as exc:
        print(f"store: could not record the outcome of {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)


def _trace_path(detail: Any) -> Path | None:
    """The trace file a call's `detail_json` names, or None when it names none. Accepts the JSON text or a decoded object."""
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return None
    trace = detail.get("trace") if isinstance(detail, dict) else None
    return Path(trace) if isinstance(trace, str) and trace else None


def _read_events(text: str) -> list[dict[str, Any]]:
    """The JSON-object lines of a trace file's text; a blank, malformed or non-object line is skipped."""
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _compact_traces(
    store: Store,
    run_id: str,
    runs_dir: Path,
    traces_url: str | None = None,
    object_store: Mapping[str, Any] | None = None,
) -> None:
    """Write this run's trace files as one Parquet file; delete them only once write_run's row count equals the events read.

    Warns and never raises. Any failure leaves every loose file, so a rerun rewrites the same Parquet file.
    """
    try:
        rows = node_calls(store.conn, run_id)
    except Exception as exc:
        print(f"traces: could not read the calls of {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)
        return
    calls: dict[str, list[dict[str, Any]]] = {}
    files: list[Path] = []
    day = ""
    for row in rows:
        path = _trace_path(row["detail_json"])
        if path is None or not path.is_file():
            continue
        try:
            calls[str(row["call_id"])] = _read_events(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"traces: could not compact {path}: {' '.join(str(exc).split())}", file=sys.stderr)
            continue
        files.append(path)
        day = day or str(row["ts"])[:10]
    try:
        if not calls:
            return
        if not have_pyarrow():
            print("traces: not compacted, writing traces as Parquet needs pyarrow: install the traces extra", file=sys.stderr)
            return
        try:
            root = resolve_traces_root(traces_url, runs_dir, os.environ, object_store)
            written = store_traces.write_run(root, day, run_id, calls)
        except store_traces.ParquetUnavailable as exc:
            print(f"traces: not compacted, {exc}", file=sys.stderr)
            return
        except Exception as exc:
            reason = " ".join(str(exc).split())
            shown = reason.replace(traces_url, redact_url(traces_url)) if traces_url else reason
            print(f"traces: could not compact {run_id}: {shown}", file=sys.stderr)
            return
        expected = sum(len(events) for events in calls.values())
        if written != expected:
            print(f"traces: not compacted, {run_id} wrote {written} rows for {expected} events", file=sys.stderr)
            return
        for path in files:
            try:
                path.unlink()
            except OSError as exc:
                print(f"traces: could not remove {path}: {' '.join(str(exc).split())}", file=sys.stderr)
    finally:
        with contextlib.suppress(OSError):
            (runs_dir / f"{run_id}-trace").rmdir()  # only succeeds on an empty directory


def _usage_line(summary: Mapping[str, Any] | None, models: Sequence[Mapping[str, Any]]) -> str | None:
    """The run's totals as one log line; None when the run recorded no calls.

    `models` are cost_by_model rows, one per (alias, tier); the line has one entry per alias, as the file ledger had.
    """
    if summary is None or not summary["calls"]:
        return None
    aliases = list(dict.fromkeys(m["model_alias"] for m in models))
    merged = [
        (a, sum(m["calls"] for m in models if m["model_alias"] == a), sum(m["cost_usd"] for m in models if m["model_alias"] == a))
        for a in aliases
    ]
    breakdown = ", ".join(f"{a}: {n} call(s) ${round(cost, 4)}" for a, n, cost in merged)
    cached, total = summary["cache_read_tokens"], summary["input_total"]
    share = f", {100 * cached // total}% of input was cache reads" if total else ""
    return f"  usage   : {summary['calls']} node call(s), {summary['turns']} turns, ${round(summary['cost_usd'], 4)} — {breakdown}{share}"


def _undercount_note(run_id: str, stored: int, seen: int) -> str | None:
    """A warning when the runner made more calls than the store holds; a runner's failed store write only logs."""
    return f"store: holds {stored} of the {seen} call(s) {run_id} made; the usage totals undercount" if seen > stored else None


def _print_usage(store: Store, run_id: str, runner: Any) -> None:
    """Print the run's totals from the store. A store that fails here warns; it never changes the run's exit."""
    try:
        summary = run_summary(store.conn, run_id)
        models = cost_by_model(store.conn, run_id)
    except Exception as exc:
        print(f"store: could not read the usage of {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)
        return
    line = _usage_line(summary, models)
    if line is not None:
        print(line)
    note = _undercount_note(run_id, summary["calls"] if summary else 0, len(getattr(runner, "calls", None) or []))
    if note is not None:
        print(note, file=sys.stderr)


def _materialise(spec: GraphSpec, args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Turn a spec's declared needs into graph args. All I/O happens HERE.

    The spec says `json_file`; the harness reads and parses the file. The graph
    module never touches the filesystem, which is what lets the portability
    suite hold it to that.
    """
    out: dict[str, Any] = {}
    for need in spec.needs:
        raw = getattr(args, need.flag.lstrip("-").replace("-", "_"), None)
        if raw is None:
            if need.required:
                parser.error(f"{spec.name} needs {need.flag}" + (f" ({need.help})" if need.help else ""))
            continue
        if need.kind == "json_file":
            out[need.name] = json.loads(Path(raw).read_text(encoding="utf-8"))
        elif need.kind == "jsonl_file":
            # One JSON object per line — the ledger's own dialect, parsed with
            # the ledger's own reader so a bad line is named the same way
            # everywhere. The graph gets rows; the file stays on this side.
            out[need.name] = list(ledger.read(raw))
        elif need.kind == "text_or_path":
            path = Path(raw)
            out[need.name] = path.read_text(encoding="utf-8") if path.is_file() else raw
        elif need.kind == "int":
            out[need.name] = int(raw)
        else:
            out[need.name] = raw
    return out


def _cos_docket_args(*, cartridge: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """The kwargs the cos arm hands `assemble_docket` — pulled out so the wire
    from `--runs-dir` (and the cartridge) into the docket it builds is one
    function a test can call directly, rather than something only a full CLI
    invocation could exercise.
    """
    from harness.cos import stranded_count

    intake_root = next(
        (
            entry.get("path")
            for entry in cartridge.get("intake") or []
            if isinstance(entry, Mapping) and entry.get("source") == "queue_dir" and entry.get("path")
        ),
        None,
    )
    return {
        "intake_root": intake_root,
        "ledger_path": args.ledger,
        "alerts_present": bool(getattr(args, "alerts", None)),
        "cartridge": cartridge,
        "runs_dir": args.runs_dir,
        "stranded": stranded_count(),
    }


def _words(declared: object) -> list[str]:
    """A declared command or name list as strings; a string splits like a shell line, unusable input is []."""
    if isinstance(declared, str):
        try:
            return shlex.split(declared)
        except ValueError:
            return []
    if isinstance(declared, list):
        return [str(w) for w in declared]
    return []


def _pool_setting(
    *, enabled: bool, keep: int, worktree_root: Path | str, landing_areas: Mapping[str, Any] | None
) -> PoolSetting | None:
    """The `PoolSetting` for `landing_areas.sync` and `.lockfiles`, or None when off.

    No sync command or no lock names also means off: without lock names the pool cannot tell a stale environment.
    """
    from harness.epic import PoolSetting

    areas = landing_areas or {}
    argv, locks = _words(areas.get("sync")), _words(areas.get("lockfiles"))
    if not enabled or not argv or not locks:
        return None
    return PoolSetting(
        pool_root=Path(worktree_root).expanduser().parent / "_pool", sync_argv=argv, lock_names=locks, keep=keep
    )


def _non_negative(text: str) -> int:
    """argparse type: a pool keep limit of 0 or more; a negative would trim every idle worktree."""
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def _read_overlay(repo: str | None) -> Any | None:
    """Parsed `<repo>/.agent/cartridge.yaml`, or None when repo or the file is absent."""
    if not repo or not Path(overlay_path(repo)).is_file():
        return None
    return yaml.safe_load(Path(overlay_path(repo)).read_text(encoding="utf-8"))


def _lifecycle_worktree(args: argparse.Namespace, cartridge: Mapping[str, Any], run_id: str) -> Path:
    """Where the lifecycle graph's own worktree lives: `<worktree_root>/<run_id>`.

    The one formula `_run_graph`'s check arm and its post-gate apply arm both
    need to create the worktree, and `main`'s exit-time cleanup needs to find
    the same directory again — kept here once so creation and cleanup can
    never compute two different paths for the same run.
    """
    root = args.worktree_root or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees")
    return Path(str(root)).expanduser() / run_id


def _checks_env(environ: Mapping[str, str], cores: int) -> dict[str, str] | None:
    """`check_env` over the capacity COX_LANE_CAPACITY states; None unless `lane_workers_from` accepts it."""
    stated = lane_workers_from(environ, cores) is not None
    return check_env(environ, cores, int(environ[LANE_CAPACITY_VAR]) if stated else None)


def _run_worktree_checks(
    worktree: Path, checks_config: Sequence[Mapping[str, Any]], log_dir: Path
) -> list[dict[str, Any]]:
    """The edge: reads the process environment and core count so the checks run under the lane cap."""
    env = _checks_env(os.environ, os.cpu_count() or 1)
    return run_checks(worktree, checks_config, log_dir=log_dir, log_prefix="checks/", env=env)


def pgrep_pids(text: str) -> list[int]:
    """The pids in `pgrep` output, one per line; blank and non-numeric lines are skipped."""
    return [int(line) for line in map(str.strip, text.splitlines()) if line.isdigit()]


def _pgrep_children(pid: int) -> str | None:
    """`pgrep -P <pid>` output; "" when exit 1 says no children, None when pgrep is missing or fails."""
    try:
        done = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return {0: done.stdout, 1: ""}.get(done.returncode)


def _child_pids(
    tasks: Path = Path("/proc/self/task"), pgrep: Callable[[int], str | None] | None = None
) -> list[int]:
    """This process's direct children, from every thread's Linux `children` file.

    Where `tasks` does not exist (macOS has no /proc) and `pgrep` is given, its output for this pid is parsed instead.
    """

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii")
        except OSError:
            return ""

    if pgrep is not None and not tasks.exists():
        return pgrep_pids(pgrep(os.getpid()) or "")
    return sorted({int(pid) for path in tasks.glob("*/children") for pid in read(path).split()})


def _terminate_children() -> None:
    for pid in _child_pids(pgrep=_pgrep_children):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


def _workers_alive() -> bool:
    return any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread())


def _exit_on_sigterm(signum: int, frame: object) -> None:
    """Ignore further SIGTERMs, stop in-flight nodes, and exit 143 (128 + SIGTERM) through `_main`'s `finally`."""
    # Marked first, so a worker that sees its child die after this point also sees the flag.
    mark_terminating()
    # Ignored next, so a supervisor's repeat SIGTERM cannot cut the `finally` that stamps `ended_at`.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    # Fan-out nodes run `subprocess.run` in worker threads, which never see this exception,
    # so their children are stopped here rather than by `subprocess.run`'s own cleanup.
    _terminate_children()
    raise SystemExit(143)


def _terminated(exc: BaseException) -> bool:
    """True for the exit `_exit_on_sigterm` raises."""
    return isinstance(exc, SystemExit) and exc.code == 143


def _stop_children_until_quiet(deadline_s: float = 30.0, poll_s: float = 0.1) -> None:
    """Keep stopping children until no worker thread is left, so a node retry cannot outlive the run."""
    end = time.monotonic() + deadline_s
    while _workers_alive() and time.monotonic() < end:
        _terminate_children()
        time.sleep(poll_s)


def main(argv: list[str] | None = None) -> int:
    # Python's default SIGTERM action is to die without running any `finally`,
    # which would leave the run's `ended_at` unset. Only the main thread may
    # install a handler; the previous one is put back on every way out.
    if threading.current_thread() is not threading.main_thread():
        return _main(argv)
    previous = signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        return _main(argv)
    except SystemExit as exc:
        # `ended_at` is already stamped; interpreter exit would otherwise wait on fan-out workers.
        if exc.code == 143:
            _stop_children_until_quiet()
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def _refuse_phase_outside_epic(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """`--phase` selects phases of an initiative, which only the epic driver has."""
    if args.graph != "epic" and getattr(args, "phase", None):
        parser.error("--phase applies to the epic graph only")


def _expand_repo(args: argparse.Namespace) -> argparse.Namespace:
    """A copy of `args` whose `repo`, when given, has a leading tilde expanded to the home directory."""
    if not args.repo:
        return args
    expanded = copy.copy(args)
    expanded.repo = str(Path(args.repo).expanduser())
    return expanded


def _main(argv: list[str] | None) -> int:
    specs = discover()
    parser = _build_parser(specs)
    parsed = parser.parse_args(argv)
    _refuse_phase_outside_epic(parser, parsed)
    args = _expand_repo(parsed)
    # A rescue's stdout is its JSON report and nothing else, since its caller parses all of it.
    # Every other line the launch prints, the usage totals included, goes to stderr instead.
    report_out = sys.stdout
    quiet = contextlib.redirect_stdout(sys.stderr) if args.graph == "rescue" else contextlib.nullcontext()
    with quiet:
        return _launch(specs, parser, args, report_out=report_out)


# Exit code to the run row's status. 3 is epic.EXIT_PAUSED and 4 is the awaiting-land code; the
# latter's constant does not exist in epic.py yet, so both stay literals here. Any other nonzero is "failed".
_EXIT_RUN_STATUS = {0: "ok", 3: "paused", 4: "awaiting_land"}


def _head_branch(cwd: str) -> str | None:
    """The branch HEAD names in `cwd`, following a worktree's `.git` pointer file. None on a detached HEAD or any read failure."""
    try:
        git = Path(cwd) / ".git"
        gitdir = git
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            gitdir = git.parent / pointer[len("gitdir:") :].strip()
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix) :] if head.startswith(prefix) else None


def _is_running(tracker: node_progress.Tracker, run_id: str, node: str, attempt: int) -> bool:
    return any(
        n["node"] == node and n["attempt"] == attempt and n["status"] == "running"
        for n in tracker.snapshot(run_id)["nodes"]
    )


def _write_progress(tracker: node_progress.Tracker, run_id: str, path: Path, lock: threading.Lock) -> str:
    """Write the snapshot to `path` and return its JSON. The lock keeps an older snapshot from replacing a newer file."""
    with lock:
        snapshot = tracker.snapshot(run_id)
        with contextlib.suppress(OSError):
            node_progress.write_atomic(path, snapshot)
    return json.dumps(snapshot)


def _node_phase(info: dict[str, Any], run_id: str) -> str:
    """The phase of a node's task: the info's own, else the directory under the run directory holding its worktree."""
    named = info.get("phase")
    if isinstance(named, str) and named:
        return named
    cwd = Path(str(info.get("cwd") or ""))
    return cwd.parent.name if cwd.parent.parent.name == run_id else ""


def _progress_observer(
    tracker: node_progress.Tracker, run_id: str, path: Path, lock: threading.Lock, *, traced: bool
) -> Callable[..., None]:
    """The runner's `on_node`: start and finish rewrite the file, events only fold into the tracker."""

    def on_node(kind: str, info: dict[str, Any], event: dict[str, Any] | None) -> None:
        node, attempt = info["role"], info["attempt"]
        if kind == "started":
            tracker.start(
                node,
                attempt,
                info["task"],
                info["tier"],
                info["cwd"],
                _head_branch(info["cwd"]),
                phase=_node_phase(info, run_id),
            )
            _write_progress(tracker, run_id, path, lock)
        elif kind == "event" and event is not None:
            tracker.observe(node, attempt, event)
        elif kind == "finished":
            # Traced, a result event has already closed the node, so one still running timed out or raised.
            # Untraced, no event arrives and the outcome is unknown, so the node is left as it is.
            if traced and _is_running(tracker, run_id, node, attempt):
                tracker.fail(node, attempt)
            _write_progress(tracker, run_id, path, lock)

    return on_node


def _launch(
    specs: dict[str, GraphSpec], parser: argparse.ArgumentParser, args: argparse.Namespace, *, report_out: TextIO
) -> int:
    if not args.skills_root and not args.unverified_skills:
        parser.error("pass --skills-root at least once, or --unverified-skills to skip the check explicitly")

    overlay = _read_overlay(args.repo)

    try:
        cartridge, skill_index = resolve_cartridge(
            args.team,
            cartridges_dir=args.cartridges_dir,
            skills_root=args.skills_root,
            unverified_skills=args.unverified_skills,
            overlay=overlay,
        )
    except CartridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    schema_status = _core_schema_status(core.SCHEMA_VERSION, CORE_SCHEMA)
    if schema_status is not None:
        level, message = schema_status
        print(message, file=sys.stderr if level == "fatal" else sys.stdout)
        if level == "fatal":
            return 1

    run_id = args.run_id or f"{args.graph}-{args.date}-{uuid.uuid4().hex[:8]}"

    # The run's record is opened before any runner exists: a run whose record
    # cannot be kept must not start. `now` is read here, at the edge, and handed down.
    started_at = datetime.now(UTC).isoformat()
    host = socket.gethostname()
    store = _begin_store_run(args, specs, cartridge, run_id, started_at, host)
    if store is None:
        return 1

    # One live run per prefix. A refusal is recorded as this run's end and stops it before any graph.
    name = run_lease.lease_name(run_id)
    try:
        lease = acquire(store.conn, name, run_id, started_at, _LEASE_TTL)
    except Exception as exc:
        print(f"lease: cannot acquire {name}: {' '.join(str(exc).split())}", file=sys.stderr)
        return _refuse_start(store, run_id, "error", 1)
    if not lease.ok:
        print(f"run: {lease.holder} holds {name}; refusing to start {run_id}", file=sys.stderr)
        return _refuse_start(store, run_id, "refused", 2)
    # Everything from here to the run's own `finally` can raise or take a SIGTERM: the started
    # marker, the heartbeat build and the setup. That exit still stamps the run and frees the
    # lease, or a relaunch would be refused by a run that is gone.
    heartbeat: run_lease.Heartbeat | None = None
    try:
        # `cox route launch` reads this line from the run log (its _LAUNCH_STARTED_MARKER); flushed because stdout is a file there.
        print(f"graph started: {run_id}", flush=True)

        # The heartbeat renews on its own connection from the moment the lease is held, so a slow
        # setup cannot eat the ttl. The epoch fence, not this thread, stops a run that lost the lease.
        def clock() -> str:
            return datetime.now(UTC).isoformat()

        tracker = node_progress.Tracker(clock)
        progress_path = Path(args.runs_dir) / f"{run_id}.progress.json"
        # Set once the runner accepts `on_node`. Until then (and for a runner that never does) a tick
        # stores no progress and writes no file.
        reporting = threading.Event()
        progress_lock = threading.Lock()
        heartbeat = run_lease.Heartbeat(
            _storage_url(_read_profile(args.provider_profile), args.runs_dir),
            name,
            run_id,
            lease.epoch,
            clock=clock,
            progress=lambda: (
                _write_progress(tracker, run_id, progress_path, progress_lock) if reporting.is_set() else None
            ),
        )
        heartbeat.start()
        runner = build_runner(
            scripted=args.scripted,
            provider_profile=args.provider_profile,
            role_skills=role_skill_bodies(cartridge, skill_index),
            workdir=args.workdir,
            repo=args.repo,
        )

        # A runner that still keeps a per-call ledger needs to know where and under
        # what name. Nothing here reads that ledger any more; usage comes from the store.
        if hasattr(runner, "runs_dir"):
            runner.runs_dir = Path(args.runs_dir)
        if hasattr(runner, "run_id"):
            runner.run_id = run_id
        if hasattr(runner, "store"):
            runner.store = store
        if hasattr(runner, "node_cap_usd"):
            runner.node_cap_usd = args.node_cap_usd
        if hasattr(runner, "on_node"):
            runner.on_node = _progress_observer(
                tracker, run_id, progress_path, progress_lock, traced=getattr(runner, "trace_dir", None) is not None
            )
            reporting.set()

        # A runner whose nodes can read the world gets a tool-computed map of it
        # first, so no node pays turns to draw one. The epic driver refreshes it per
        # phase; this is the single-graph case.
        if args.repo and hasattr(runner, "repo_digest"):
            runner.repo_digest = build_digest(Path(args.repo)) or None
        if hasattr(runner, "check_commands"):
            checks = (cartridge.get("landing_areas") or {}).get("checks") or []
            runner.check_commands = [str(c.get("cmd")) for c in checks if isinstance(c, dict) and c.get("cmd")]
        if hasattr(runner, "gate_check_commands"):
            by_gate = gate_commands(cartridge.get("landing_areas") or {})
            runner.gate_check_commands = {g: cmds for g, cmds in by_gate.items() if g != "task"}

        # `lifecycle` is the only graph this file ever creates a worktree for
        # (the check arm and the post-gate apply arm inside `_run_graph`, both via
        # `_lifecycle_worktree`); `epic`, `phase` and `cos` never reach that code,
        # so there is nothing here for them to clean up.
        worktree = _lifecycle_worktree(args, cartridge, run_id) if args.graph == "lifecycle" else None
    except BaseException as exc:
        _finish_store_run(store, run_id, datetime.now(UTC).isoformat(), "error")
        if _terminated(exc):
            _stop_children_until_quiet()
        _end_lease(store, heartbeat, name, run_id, lease.epoch)
        store.conn.close()
        raise

    # Anything that leaves `_run_graph` without returning (a parser error, a raise) is "error".
    status = "error"
    # Set only by a SIGTERM exit. The edge's one flag: the `finally` must quiet the workers before it frees the lease.
    terminated = False
    try:
        if args.graph == "rescue":
            # The rescue names its row's status itself: a LimitStop is `stopped` yet exits 0, since the report says so.
            code, status = _run_rescue(
                parser=parser,
                args=args,
                cartridge=cartridge,
                runner=runner,
                run_id=run_id,
                store=store,
                epoch=lease.epoch,
                lease_name=name,
                report_out=report_out,
            )
            return code
        code = _run_graph(
            specs=specs,
            parser=parser,
            args=args,
            cartridge=cartridge,
            runner=runner,
            run_id=run_id,
            store=store,
            epoch=lease.epoch,
            lease_name=name,
        )
        status = _EXIT_RUN_STATUS.get(code, "failed")
        return code
    except SystemExit as exc:
        terminated = _terminated(exc)
        raise
    finally:
        # First, so the run's end is on record even if a later step here raises.
        _finish_store_run(store, run_id, datetime.now(UTC).isoformat(), status)
        # On SIGTERM the prefix stays held until the run's own workers are quiet, so a
        # successor never starts beside them. `main` repeats this wait; by then it finds none.
        if terminated:
            _stop_children_until_quiet()
        _end_lease(store, heartbeat, name, run_id, lease.epoch)
        # Every exit below — success, a caught exception's `return 1`, or
        # anything left to raise past this point — prints the run's totals from
        # the store, which holds every call recorded so far. Guarded like
        # `_finish_store_run`: a store fault warns and never skips what follows.
        # `close` runs AFTER, the order the single-graph path always had: the
        # runner's `calls` must still be readable for the undercount check.
        _print_usage(store, run_id, runner)
        close = getattr(runner, "close", None)
        if callable(close):
            close()
        # Compaction only reads the store and moves files, so it never changes the exit code.
        try:
            profile = _read_profile(args.provider_profile)
            _compact_traces(
                store,
                run_id,
                Path(args.runs_dir),
                _traces_url(profile, args.runs_dir),
                _object_store(profile),
            )
        except Exception as exc:
            print(f"traces: compaction failed: {' '.join(str(exc).split())}", file=sys.stderr)
        store.conn.close()
        # Cleanup runs last — quarantine, a caught exception's `return 1`, or
        # anything still raising past this point — so a run never leaves its
        # worktree behind for a human to notice. Guarded on the directory
        # actually existing: a lifecycle run whose build produced no patch,
        # or whose patch never reached an approved gate decision, never
        # created one, and cleaning up a path that was never made would
        # report a false "removed" or "FAILED to keep" for a run that did
        # nothing wrong. `repo` is --repo when the worktree is a real `git
        # worktree` of it (the check arm's `create_worktree`); when --repo
        # was never given, `worktree` is its own standalone `git init` repo
        # (the scratch dir `apply_patch` falls back to), never a linked
        # worktree of this harness's own checkout, so `worktree` itself — not
        # REPO_ROOT — is the only repo the cleanup call could mean there.
        if worktree is not None and worktree.exists():
            repo = Path(args.repo) if args.repo else worktree
            if args.keep_worktrees:
                ok, detail = keep_worktree(repo, worktree, worktree.parent, run_id)
                print(f"worktree {'kept' if ok else 'FAILED to keep'}: {detail}", file=sys.stderr if not ok else sys.stdout)
            else:
                ok, detail = remove_worktree(repo, worktree)
                print(f"worktree {'removed' if ok else 'FAILED to remove'}: {detail}", file=sys.stderr if not ok else sys.stdout)


def _write_filed_task(runs_dir: Path, path: Path, parts: tuple[str, ...], text: str) -> int:
    """Edge: `route file`'s own writer. The store row first, the file second, then the export baseline."""
    from agent_tools import route  # imported here: the harness stays importable without the tools package

    return route.write_filed_item(runs_dir, path, "task", parts, text)


def _read_filed_rows(runs_dir: Path, initiative: str) -> list[dict[str, Any]]:
    """Edge: the queue rows `route edit` would see, read by the same path `route file` writes them through."""
    from agent_tools import run_store  # imported here: the harness stays importable without the tools package

    return run_store.read_queue(runs_dir, initiative)


def _withdraw_filed_tasks(runs_dir: Path, root: Path, initiative: str, rels: Sequence[str], whole: bool) -> list[str]:
    """Edge: the `rels` actually withdrawn; a file goes only after its store row is dropped, else it is named and kept."""
    # imported here: the harness stays importable without the tools package
    from agent_tools import draft_apply, run_store, store_cli

    if whole and rels:
        store = draft_apply.Store(
            "store",
            lambda name: run_store.work_items(runs_dir, name),
            lambda name, task, state, by, expected: store_cli.set_state(runs_dir, name, task, state, by, expected),
        )
        work_dir = root / Path(rels[0]).parent.parent.parent
        if draft_apply.remove(work_dir, initiative, "decompose left a task missing", "decompose", store) == 0:
            for rel in rels:
                (root / rel).unlink(missing_ok=True)
            return list(rels)
    stored = {r["task_id"] for r in run_store.work_items(runs_dir, initiative)}
    done: list[str] = []
    for rel in rels:
        task = Path(rel).stem
        result = store_cli.set_state(runs_dir, initiative, task, "dropped", "decompose") if task in stored else None
        if task in stored and not isinstance(result, store_cli.StateSet):
            print(f"decompose: {task} not withdrawn: its store row was not dropped ({result})", file=sys.stderr)
            continue
        (root / rel).unlink(missing_ok=True)
        done.append(rel)
    return done


def _decompose_phase_dirs(
    proposals: Sequence[Mapping[str, Any]], cartridge: Mapping[str, Any], initiative_id: str | None
) -> list[str]:
    """Store-relative task phase directories; none when no `planned` landing routes, as `_settle_decompose` reports."""
    try:
        landing = landing_for(cartridge, "planned")
    except ContractViolation:
        return []
    return phase_dirs(proposals, landing=landing, initiative_id=initiative_id)


def _make_phase_dirs(root: Path, rels: Sequence[str]) -> None:
    """Edge: the model `work_item_arm` Write fails ENOENT on an absent phase directory, so create each first."""
    for rel in rels:
        (root / rel).mkdir(parents=True, exist_ok=True)


def _repo_trees(
    repo_map: Mapping[str, Any],
    workspace: Path,
    target: str | None,
    *,
    home: str,
    is_checkout: Callable[[Path], bool],
    listing: Callable[[str], Sequence[Mapping[str, str]]],
) -> dict[str, list[str]]:
    """Repository path to its tracked paths for each `repo_map` value that is a checkout other than `target`.

    A value resolves as `route.resolve_repo` does, a bare name against the workspace's parent directory."""
    from agent_tools import route  # imported here: the harness stays importable without the tools package

    own = Path(target).expanduser().resolve() if target else None
    trees: dict[str, list[str]] = {}
    for value in repo_map.values():
        if not isinstance(value, str):
            continue
        path = route.resolve_repo(value, workspace.parent, is_checkout, home)
        if isinstance(path, route.RepoRefused) or not is_checkout(Path(path).expanduser()):
            continue
        if own is not None and Path(path).expanduser().resolve() == own:
            continue
        trees[path] = [str(row["path"]) for row in listing(path)]
    return trees


def _decompose_repo_trees(profile: Mapping[str, Any], workspace: Path, target: str | None) -> dict[str, list[str]]:
    """Edge: `_repo_trees` over the profile's `repo_map`; empty when the profile has none."""
    from agent_tools import route  # imported here: the harness stays importable without the tools package
    from graphs.delivery.initiative_decompose import repo_listing

    repo_map = profile.get("repo_map")
    if not isinstance(repo_map, Mapping):
        return {}
    return _repo_trees(
        repo_map, workspace, target, home=str(Path.home()), is_checkout=route.is_git_checkout, listing=repo_listing
    )


def _file_decompose_follow_ups(
    result: Mapping[str, Any], *, initiative: str, workspace: Path, runs_dir: Path, date: str
) -> int:
    """Edge: file each follow-up as an intake waiting on `initiative`; the count whose write failed.

    The queue treats a landed initiative's id as met, so `initiative` is the id the intake waits on."""
    from agent_tools import follow_up_intake  # imported here: the harness stays importable without the tools package

    failed = 0
    for follow_up in result.get("follow_ups") or []:
        rel = follow_up_intake.file_follow_up(
            runs_dir, workspace, follow_up, after=initiative, date=date, home=str(Path.home())
        )
        if rel is None:
            print(f"decompose: follow-up for {follow_up['repo']} was not filed", file=sys.stderr)
            failed += 1
        else:
            print(f"decompose: filed follow-up for {follow_up['repo']}: {rel}")
    return failed


def _settle_decompose(
    proposals: Sequence[Mapping[str, Any]],
    diffs: Sequence[Mapping[str, Any]],
    auto_applied: Sequence[Mapping[str, Any]],
    *,
    cartridge: Mapping[str, Any],
    graph_args: Mapping[str, Any],
    runner: Any,
    runs_dir: Path,
    withdraw: Callable[[Path, Path, str, Sequence[str], bool], Sequence[str]] | None = None,
) -> tuple[str, int]:
    """Edge: settle every task `item_create` that was cleared, gated or auto-applied. `(closing line, missing count)`.

    A cartridge that routes no `planned` landing cannot say where a task file went, so every cleared
    task is reported missing with the routing error; it is never reported as zero tasks.
    When any task is missing, the tasks this run created are withdrawn so no initiative keeps a
    `needs` on an absent task; a task already in the store keeps its row. The count is what remains.
    """
    tasks = approved_task_ids(proposals, diffs, auto_applied)
    initiative_id = graph_args.get("initiative_id")
    root = Path(getattr(runner, "cwd", None) or Path.cwd())
    try:
        landing = landing_for(cartridge, "planned")
    except ContractViolation as exc:
        if tasks:
            print(f"decompose: cannot place {len(tasks)} approved task(s): {exc}", file=sys.stderr)
        return decompose_closing(tasks, (), ())
    expected = expected_task_files(proposals, tasks, landing=landing, initiative_id=initiative_id)
    initiative = initiative_id or root.name

    # a dropped row is a withdrawn task, so a retry writes it afresh
    def live(rd: Path, name: str) -> list[dict[str, Any]]:
        return [r for r in _read_filed_rows(rd, name) if r.get("state") != "dropped"]

    before_rows = live(runs_dir, initiative)
    before = {r["task_id"] for r in before_rows}
    in_store, on_disk = _settle_decompose_tasks(
        expected, root=root, runs_dir=runs_dir, initiative=initiative, read=live
    )
    missing = sorted(task for task in expected if task not in in_store or task not in on_disk)
    gone = withdrawal(expected, before, missing)
    withdraw_fn = withdraw or _withdraw_filed_tasks
    withdrawn: set[str] = set()
    if gone:
        # whole only when this run's own initiative item_create cleared and no task row existed before it
        made = any(p.get("kind") == "item_create" and p.get("target") == "initiative" for p in auto_applied) or any(
            d.get("kind") == "item_create" and d.get("target") == "initiative" and d.get("decision") == "approved"
            for d in diffs
        )
        done = withdraw_fn(runs_dir, root, initiative, [expected[t] for t in gone], made and not before)
        withdrawn = {Path(rel).stem for rel in done}
        print(f"decompose: missing {', '.join(missing)}; withdrew {', '.join(sorted(withdrawn))}", file=sys.stderr)
    if missing:
        # a stored row's needs win over the proposal's for a task this run did not create
        needs = {**_task_needs(proposals, expected), **{r["task_id"]: list(r.get("needs") or []) for r in before_rows}}
        # a withdrawn task is gone too, so a kept task that needs one dangles just as it would on a missing one
        withdrawn |= _withdraw_dependents(
            needs,
            {*missing, *withdrawn},
            [t for t in {*expected, *before} if t not in missing and t not in withdrawn],
            before,
            expected,
            withdraw=lambda rels: withdraw_fn(runs_dir, root, initiative, rels, False),
        )
        print(f"decompose: partial: initiative {initiative} missing {', '.join(missing)}", file=sys.stderr)
    kept = {task: rel for task, rel in expected.items() if task not in withdrawn or task in missing}
    return decompose_closing(kept, in_store - withdrawn, on_disk - withdrawn)


def _dependents_of_missing(
    needs: Mapping[str, Sequence[str]], missing: Collection[str], candidates: Collection[str]
) -> list[str]:
    """Sorted candidates whose needs name a missing id, directly or through another such candidate."""

    def grow(hit: frozenset[str]) -> frozenset[str]:
        more = hit | {t for t in candidates if set(needs.get(t, ())) & (set(missing) | hit)}
        return hit if more == hit else grow(more)

    return sorted(grow(frozenset()))


def _action_needs(action: str) -> list[str]:
    """The `needs` in the frontmatter block of a decompose `suggested_action`; `[]` when it carries none."""
    block = action.partition("\n")[2].rpartition("body = ")[0]
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return []
    return [str(n) for n in (data.get("needs") if isinstance(data, dict) else None) or []]


def _task_needs(proposals: Sequence[Mapping[str, Any]], expected: Collection[str]) -> dict[str, list[str]]:
    """Task id to the ids named by `needs:` in its `item_create` proposal's `suggested_action`."""
    return {
        str(p["target"]): _action_needs(str(p.get("suggested_action") or ""))
        for p in proposals
        if p.get("kind") == "item_create" and str(p.get("target")) in expected
    }


def _withdraw_dependents(
    needs: Mapping[str, Sequence[str]],
    missing: Collection[str],
    candidates: Collection[str],
    before: Collection[str],
    expected: Mapping[str, str],
    *,
    withdraw: Callable[[Sequence[str]], Sequence[str]],
) -> set[str]:
    """Edge: withdraw each kept task that needs a missing id, so none is left needing one; the ids withdrawn.

    A task already in the store keeps its row and file and is named instead, as is any task the withdraw path refuses.
    """
    dependents = _dependents_of_missing(needs, missing, candidates)
    for task in (t for t in dependents if t in before):
        print(f"decompose: {task} was already in the store and needs a task that is missing or withdrawn; its row is kept", file=sys.stderr)
    sweep = [t for t in dependents if t not in before]
    done = {Path(rel).stem for rel in withdraw([expected[t] for t in sweep])} if sweep else set()
    for task in (t for t in sweep if t not in done):
        print(f"decompose: {task} needs a missing task and could not be withdrawn", file=sys.stderr)
    return done


def _settle_decompose_tasks(
    expected: Mapping[str, str],
    *,
    root: Path,
    runs_dir: Path,
    initiative: str,
    write: Callable[[Path, Path, tuple[str, ...], str], int] | None = None,
    read: Callable[[Path, str], Sequence[Mapping[str, Any]]] | None = None,
) -> tuple[set[str], set[str]]:
    """Edge: `(in_store, on_disk)` for the cleared tasks, after each file goes through `route file`'s writer.

    The apply arm writes the file and nothing else, so a task could be on disk and absent from the
    queue (`route edit` then says `no queued intake or initiative named <task>`). Each file the arm
    left is handed to `write_filed_item`, which builds the row with `queue_rows.parse_item` (extra
    frontmatter kept), rewrites the same text, and records the export baseline. That writer only
    warns when it cannot reach the store, so the row is read back through the tools' own reader,
    the one `route edit` uses, before the task counts as stored. A task already in the store keeps
    its row, so a re-decompose never resets started work. A task not in the store, or not on disk, is tried
    once more; one still missing after the second try gets a stderr line for each failed try.
    """
    write = write or _write_filed_task
    read = read or _read_filed_rows
    on_disk: set[str] = set()
    in_store: set[str] = set()
    kept = {r["task_id"]: r for r in read(runs_dir, initiative)}

    def has_row(task: str) -> bool:
        try:
            return any(r["task_id"] == task for r in read(runs_dir, initiative))
        except Exception:  # an unreadable store means no row can be vouched for
            return False

    def attempt(task: str, rel: str, retry: bool = False) -> tuple[bool, bool, str]:
        """One read-and-write try: `(on_disk, in_store, stderr line)`; the line is empty when the task is in the store."""
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return False, False, f"decompose: {task} not written: {exc} (the apply line above carries the arm's own detail)"
        if task in kept:
            print(f"decompose: {task} was already in the store as '{kept[task].get('state')}'; its row is kept", file=sys.stderr)
            return True, True, ""
        try:
            code = write(runs_dir, path, (initiative, Path(rel).parent.name, Path(rel).name), text)
            stored = any(r["task_id"] == task for r in read(runs_dir, initiative))
        except Exception as exc:  # the driver's errors differ by dialect; any one means the row is not in
            left = " (the first try left its row in the store)" if retry and has_row(task) else ""
            return True, False, f"decompose: {task} is on disk but the write failed: {type(exc).__name__}: {exc}{left}"
        if code != 0:
            return True, False, f"decompose: {task} is in the store but its file write failed (exit {code})"
        if not stored:
            return True, False, f"decompose: {task} is on disk but the store has no row for it after the write"
        return True, True, ""

    for task, rel in sorted(expected.items()):
        tries = [attempt(task, rel)]
        if not tries[0][1]:  # the one retry: a task still missing after the second try is missing
            tries.append(attempt(task, rel, retry=True))
        disk, stored, _ = tries[-1]
        if disk:
            on_disk.add(task)
        if stored:
            in_store.add(task)
        else:
            for _, _, line in tries:
                print(line, file=sys.stderr)
    return in_store, on_disk


def _record_run_to_store(
    store: Store,
    manifest: Mapping[str, Any],
    diffs: Sequence[Mapping[str, Any]],
    *,
    graph_name: str,
    ledger_path: Path | str,
) -> None:
    """Record a run as the epic driver records a phase: one phases row, its gate decisions, its ledger rows.

    A store error warns and never changes the exit code.
    """
    run_id = str(manifest["run_id"])
    phase_run_id = f"{run_id}:{graph_name}"
    row = {
        "run_id": phase_run_id,
        "phase": graph_name,
        "ts": manifest["ts"],
        "principal": manifest["principal"],
        "human_minutes": manifest["human_minutes"],
        "totals": manifest["totals"],
        "manifest": phase_run_id,
        "manifest_record": dict(manifest),
    }
    try:
        for entry in ledger.read(ledger_path):
            if entry.get("run_id") == run_id:
                store.record_ledger(entry)
        store.record_gate_decisions(run_id, graph_name, diffs)
        store.record_phase(row)
    except Exception as exc:
        print(f"store: could not record {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)


def _with_stored_causes(item: Mapping[str, Any], rows: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """`item` with each attempt's `cause` taken from the (run_id, ts, cause) row it matches; a work file holds no cause."""
    causes = {(run, ts): cause for run, ts, cause in rows}
    return {
        **item,
        "attempts": [{**a, "cause": causes.get((a.get("run"), a.get("ts")), a.get("cause"))} for a in item.get("attempts") or []],
    }


def _run_rescue(
    *,
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    cartridge: Mapping[str, Any],
    runner: Any,
    run_id: str,
    store: Store,
    epoch: int | None,
    lease_name: str | None,
    report_out: TextIO,
) -> tuple[int, str]:
    """(exit, run status). Exit 0 with a JSON report for any answer, LimitStop included; 1 for a task not in the initiative."""
    # Not module-level: harness/epic imports this package (see the epic branch of `_run_graph`).
    from harness.epic import _Ctx, _mirror_read, rescue_task, resolve_default_ref, work_state_of

    for flag, value in (("--initiative", args.initiative), ("--task", args.task), ("--repo", args.repo)):
        if not value:
            parser.error(f"rescue needs {flag}")
    try:
        initiative = workstore.read_initiative(args.initiative)
    except workstore.WorkStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1, "failed"
    found = next((i for i in initiative["items"] if str(i.get("id")) == args.task), None)
    if found is None:
        print(f"task {args.task} is not in {args.initiative}", file=sys.stderr)
        return 1, "failed"
    try:
        work_state = work_state_of(_read_profile(args.provider_profile))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1, "failed"

    # The default branch, read as `run_epic` reads it, so a rescue checks against the ground an epic run would.
    repo = Path(args.repo)
    try:
        agent_checks = (repo / ".agent-checks").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        agent_checks = ""
    ctx = _Ctx(
        repo=repo,
        cartridge=cartridge,
        runner=runner,
        specs={},
        run_id=run_id,
        date=args.date,
        max_parallel=1,
        ledger_path=Path(args.ledger),
        provider_profile=_provider_profile_scope(args.provider_profile),
        runs_dir=Path(args.runs_dir),
        worktree_root=Path(
            str(args.worktree_root or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees"))
        ).expanduser(),
        assume=args.assume,
        fix_attempts=args.fix_attempts,
        repo_checks=repo_checks(agent_checks),
        lane_workers=lane_workers_from(os.environ, os.cpu_count() or 1),
        store=store,
        epoch=epoch,
        lease_name=lease_name,
        work_state=work_state,
        initiative_id=str(initiative.get("id")),
        default_ref=resolve_default_ref(repo, getattr(args, "base", None)),
    )
    # As `run_epic` does before any work: under work_state store the row's state wins, so the move's compare-and-set holds.
    items = [dict(found)]
    _mirror_read(ctx, items)
    mark = store.conn.dialect.placeholder
    stored = store.conn.query_all(f"SELECT run_id, ts, cause FROM attempts WHERE task_id = {mark}", (args.task,))
    try:
        report, status = rescue_task(ctx, _with_stored_causes(items[0], stored), phase=str(found["phase"])), "ok"
    except LimitStop as exc:
        report, status = {"status": "stopped", "why": exc.detail}, "stopped"
    print(json.dumps(report, sort_keys=True), file=report_out)
    return 0, status


def register_stack_dump(stream: TextIO) -> None:
    """Dump every thread's stack to `stream` on SIGUSR1, without pausing or killing the process.

    Leaves `faulthandler.is_enabled()` (the fault handler's own enabled state, for
    SIGSEGV/SIGABRT/etc.) untouched — this registers a handler for a user signal only.
    `faulthandler.register` needs a real file descriptor; a stream that has none (a
    captured stdout under a test runner, say) means there is nowhere to dump to, so
    registration is skipped rather than taking the run down over it.
    """
    try:
        stream.fileno()
    except (OSError, AttributeError):
        return
    faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True)


def _run_graph(
    *,
    specs: dict[str, GraphSpec],
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    cartridge: Mapping[str, Any],
    runner: Any,
    run_id: str,
    store: Store | None = None,
    epoch: int | None = None,
    lease_name: str | None = None,
) -> int:
    if args.graph == "epic":
        # The whole initiative. The driver gates and records PER PHASE — phase
        # N+1's base depends on which merges the gate let into phase N's branch,
        # so one gate at the end would be deciding after the ground had already
        # been chosen. Everything below this block (policy split, gate, record)
        # is therefore already done by the time run_epic returns, and this
        # branch returns instead of falling through to do it twice.
        from harness.epic import run_epic, work_state_of

        if not args.initiative:
            parser.error("epic needs --initiative (a work/<initiative> directory)")
        if not args.repo:
            parser.error("epic needs --repo (the repository the initiative's work targets)")
        try:
            initiative = workstore.read_initiative(args.initiative)
        except workstore.WorkStoreError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        try:
            work_state = work_state_of(_read_profile(args.provider_profile))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        headroom_usd, est_task_usd = _usage_bound(parser, args, runner)

        # Wired to stdout because that's the stream this branch's own status
        # lines (the "epic {run_id}: ..." summary below) already write to —
        # the run's log, for whatever is capturing this process's output.
        register_stack_dump(sys.stdout)

        worktree_root = args.worktree_root or (cartridge.get("landing_areas") or {}).get("worktree_root", "~/worktrees")
        pool = _pool_setting(
            enabled=not getattr(args, "no_pool", False),
            keep=getattr(args, "pool_keep", DEFAULT_POOL_KEEP),
            worktree_root=worktree_root,
            landing_areas=cartridge.get("landing_areas"),
        )
        result = run_epic(
            initiative=initiative,
            repo=Path(args.repo),
            cartridge=cartridge,
            runner=runner,
            specs=specs,
            run_id=run_id,
            date=args.date,
            max_parallel=_parallel_cap(parser, args, len(initiative.get("items") or [])),
            headroom_usd=headroom_usd,
            est_task_usd=est_task_usd,
            ledger_path=args.ledger,
            provider_profile=_provider_profile_scope(args.provider_profile),
            runs_dir=args.runs_dir,
            worktree_root=worktree_root,
            assume=args.assume,
            fix_attempts=args.fix_attempts,
            resume_from=args.resume_from,
            pool=pool,
            store=store,
            epoch=epoch,
            lease_name=lease_name,
            work_state=work_state,
            base=getattr(args, "base", None),
            **({"only_phases": frozenset(args.phase)} if getattr(args, "phase", None) else {}),
        )
        totals = result.get("totals") or {}
        print(epic_summary_line(run_id, totals))
        for entry in result.get("quarantined") or []:
            print(f"  quarantined {entry.get('grain')}: {entry.get('id')} — {entry.get('reason')}", file=sys.stderr)
        print(f"  phases   : recorded in the run store under {run_id}")
        print(f"  ledger   : {args.ledger}")
        for line in result.get("exit_summary") or []:
            print(f"  {line}", file=sys.stderr)
        return int(result.get("exit_code", 0))

    if args.graph == "sweep" and "sweep" not in specs:
        # Listed in `_build_parser`'s choices (docs/design/work-shape.md §1) so
        # the coxswain and this CLI agree the name exists, but
        # graphs/ops/sweep.py declares no SPEC yet, so it never lands in
        # `specs`. Refuse here, the same way `parser.error` refuses a missing
        # `--initiative` below, rather than let `specs[args.graph]` further
        # down raise a bare `KeyError` for a choice argparse just accepted.
        # Gated on `specs` (not just the literal name) so this refusal turns
        # itself off the day a later task gives sweep a real SPEC, instead of
        # outliving its own reason and blocking a working graph forever.
        parser.error("sweep is registered but has no SPEC yet (graphs/ops/sweep.py); not runnable standalone")

    # Set only by the generic single-graph branch below; the phase and cos
    # drivers build a synthetic result and do not honour --result-out.
    result_out: str | None = None

    if args.graph == "phase":
        # Not one graph run but many, one per unblocked task. The work store is
        # read HERE and the tasks handed in as arguments, because a graph that
        # reads the filesystem cannot be replayed.
        if not args.initiative:
            parser.error("phase needs --initiative (a work/<initiative> directory)")
        try:
            initiative = workstore.read_initiative(args.initiative)
        except workstore.WorkStoreError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        # States of tasks in other initiatives, from the same load as the items:
        # a need on one is met only through this mapping.
        foreign = initiative.get("foreign") or {}
        # Resolved the way run_epic does, so an unmerged approved need is met at ticket and level, not at task.
        try:
            land_at = resolve_land_at(cartridge, initiative.get("land_at"))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        phase_name = args.phase_name
        if phase_name is None:
            phase_name = next(
                (
                    p
                    for p in initiative["phases"]
                    if workstore.ready_tasks(initiative["items"], phase=p, foreign=foreign, land_at=land_at)
                ),
                None,
            )
        ready = (
            workstore.ready_tasks(initiative["items"], phase=phase_name, foreign=foreign, land_at=land_at)
            if phase_name
            else []
        )
        if not ready:
            print(f"nothing ready in {initiative['id']}" + (f" phase {phase_name}" if phase_name else ""))
            return 0

        max_parallel = _parallel_cap(parser, args, len(ready))
        print(f"phase {phase_name}: {len(ready)} task(s) ready, running up to {max_parallel} at once")
        print("  " + ", ".join(t["id"] for t in ready))
        results, proposals, failures = run_phase(
            lifecycle_run=specs["lifecycle"].run,
            tasks=ready,
            cartridge=cartridge,
            runner=runner,
            run_id=run_id,
            date=args.date,
            max_parallel=max_parallel,
        )
        for failure in failures:
            print(f"task failed: {failure}", file=sys.stderr)
        graph_name = _PHASE_PRINCIPAL
        result = {
            "run_id": run_id,
            "phase": phase_name,
            "tasks": [r.get("ticket") for r in results],
            "proposals": proposals,
            "totals": {"ready": len(ready), "completed": len(results), "failed": len(failures)},
        }
    elif args.graph == "cos" and not getattr(args, "docket", None):
        # The coxswain driver path. With --docket the cos graph runs alone
        # through the generic arm below — judgment only, nothing invoked. Without
        # it, the driver assembles the docket from what is actually readable
        # (intake queue, ledger, registry), runs the dispatch graph, and invokes
        # what it selected through the nested-invocation primitive, so every
        # dispatched proposal lands in the same policy/gate/record as any other.
        from harness.cos import CosError, assemble_docket, run_cos

        # `--runs-dir` is the same global flag every other arm already writes
        # manifests under; reusing it here (default: the real runs directory)
        # is safe because assemble_docket only counts a live `*.pid` as
        # in flight — everything else already down there is ignored.
        docket_args = _cos_docket_args(cartridge=cartridge, args=args)
        docket = assemble_docket(specs=specs, **docket_args)
        alerts = (
            json.loads(Path(args.alerts).read_text(encoding="utf-8")) if getattr(args, "alerts", None) else None
        )
        try:
            cos_out = run_cos(
                docket=docket,
                specs=specs,
                runner=runner,
                cartridge=cartridge,
                run_id=run_id,
                date=args.date,
                max_parallel=4 if args.max_parallel is None else args.max_parallel,  # cos keeps its old default of 4
                intake_root=docket_args["intake_root"],
                ledger_path=args.ledger,
                alerts=alerts,
            )
        except (ContractViolation, RunnerError, CosError) as exc:
            print(f"coxswain failed: {exc}", file=sys.stderr)
            return 1
        for failure in cos_out["failures"]:
            print(f"dispatched run failed: {failure}", file=sys.stderr)
        deferred = cos_out["deferred"]
        invoked = cos_out["invoked"]
        if invoked:
            picked = ", ".join(str(s.get("graph")) for s in invoked)
        elif deferred:
            picked = "nothing (at capacity)"
        else:
            picked = "nothing (idle)"
        print(f"coxswain dispatched: {picked}")
        if deferred:
            reasons = "; ".join(f"{d['graph']} ({d['reason']})" for d in deferred)
            print(f"coxswain deferred: {reasons}")
        if cos_out["consumed"]:
            print(f"intake consumed: {', '.join(cos_out['consumed'])}")
        graph_name = _COS_PRINCIPAL
        result = {
            "run_id": run_id,
            "date": args.date,
            "selections": cos_out["selections"],
            "results": cos_out["results"],
            "proposals": cos_out["proposals"],
            "consumed": cos_out["consumed"],
            "deferred": deferred,
            "totals": {
                "selected": len(cos_out["selections"]),
                "completed": len(cos_out["results"]),
                "failed": len(cos_out["failures"]),
                "consumed": len(cos_out["consumed"]),
                "deferred": len(deferred),
            },
        }
    else:
        spec = specs[args.graph]
        graph_name = spec.graph_name
        result_out = getattr(args, "result_out", None)
        if args.graph == "retro" and getattr(args, "ledger_rows", None) is None:
            # The rows retro reasons over default to the ledger this harness
            # already keeps. Explicit --ledger-rows still points it anywhere —
            # a retro over some other record is a legitimate ask — but the graph
            # itself never reads either; the harness does, right here.
            args.ledger_rows = str(args.ledger)
        graph_args: dict[str, Any] = {"run_id": run_id, "date": args.date, "cartridge": cartridge}
        graph_args.update(_materialise(spec, args, parser))
        if getattr(args, "repo", None):
            graph_args.setdefault("checkout", str(Path(args.repo).expanduser()))  # the readable repo the seats get
        if graph_name == _DECOMPOSE_GRAPH_NAME:
            trees = _decompose_repo_trees(
                _read_profile(args.provider_profile) if getattr(args, "provider_profile", None) else {},
                Path(getattr(runner, "cwd", None) or Path.cwd()),
                graph_args.get("checkout"),
            )
            if trees:
                graph_args["repo_trees"] = trees
        try:
            result = spec.run(graph_args, runner)
        except LintRefusal as exc:
            # Recorded before the exit, so the run says why it refused; the exit itself is the same as below.
            _record_refusal(store, run_id, exc)
            print(f"{graph_name} failed: {exc}", file=sys.stderr)
            return 1
        except (ContractViolation, RunnerError) as exc:
            # A contract violation or a dead runner is a bad invocation, and it
            # is reported as one. Anything else is a bug in this code and is
            # allowed to raise with its traceback intact rather than be
            # flattened into "failed".
            print(f"{graph_name} failed: {exc}", file=sys.stderr)
            return 1

    provider_profile = _provider_profile_scope(args.provider_profile)
    proposals = result.get("proposals", [])

    # The check arm. Only when --repo names the project this change targets,
    # and only for the single-graph lifecycle path — the epic driver arriving
    # separately owns fanning this out across a phase's per-task results. It
    # runs BEFORE the policy and the gate see anything: evidence attached
    # after the decision is already made is decoration, not evidence.
    if args.repo and args.graph == "lifecycle" and result.get("build", {}).get("patch"):
        worktree = _lifecycle_worktree(args, cartridge, run_id)
        targets = [p for p in proposals if p.get("kind") == "draft_pr_create"]

        wt_ok, wt_detail = create_worktree(Path(args.repo), worktree, branch=f"agents/{run_id}")
        if not wt_ok:
            print(f"\nworktree FAILED for agents/{run_id}: {wt_detail}", file=sys.stderr)
            for item in targets:
                item.setdefault("evidence", []).append({"check": "patch_apply", "output": f"FAIL — {wt_detail}"})
        else:
            ok, detail = apply_patch(result["build"]["patch"], worktree)
            print(f"\npatch {'applied in' if ok else 'FAILED to apply in'} {worktree}")
            if not ok:
                print(f"  {detail}", file=sys.stderr)
                for item in targets:
                    item.setdefault("evidence", []).append({"check": "patch_apply", "output": f"FAIL — {detail}"})
            else:
                evidence_rows = [{"check": "patch_apply", "output": f"ok — applied in {worktree}"}]
                checks_config = (cartridge.get("landing_areas") or {}).get("checks") or []
                if not checks_config:
                    print("no checks configured; the gate decides on review evidence alone")
                else:
                    check_results = _run_worktree_checks(
                        worktree, checks_config, Path(args.runs_dir) / run_id / "checks"
                    )
                    result["checks"] = check_results
                    for r in check_results:
                        print(f"  check {r['name']}: {'pass' if r['passed'] else 'FAIL'} (exit {r['exit_code']})")
                    print(f"  checks overall: {'all passed' if all_passed(check_results) else 'FAILURES present'}")
                    evidence_rows.extend(checks_evidence(check_results))
                for item in targets:
                    item.setdefault("evidence", []).extend(evidence_rows)

    # Escalation, from the patch's paths alone. Here because it must land AFTER
    # the graph named its kinds and the check arm attached its verdict — the
    # gate should see the tests' opinion of a governance change too — and
    # BEFORE the policy split, which is the only window where no streak on a
    # mundane kind can carry an edit to the rules past the gate.
    if args.graph == "phase":
        escalated: list[dict[str, Any]] = []
        for r in results:
            slice_, hits = escalate_self_modification(
                r.get("proposals", []),
                patch=r.get("build", {}).get("patch") or "",
                cartridge=cartridge,
                ledger_path=args.ledger,
            )
            r["proposals"] = slice_
            if hits:
                print(_governance_line(hits, label=str(r.get("ticket") or "task")))
            escalated.extend(slice_)
        proposals = result["proposals"] = escalated
    elif result.get("build", {}).get("patch"):
        proposals, hits = escalate_self_modification(
            proposals, patch=result["build"]["patch"], cartridge=cartridge, ledger_path=args.ledger
        )
        result["proposals"] = proposals
        if hits:
            print(_governance_line(hits))

    # Consult the policy BEFORE the human sees anything. Without this the gate
    # asks about every kind forever, no streak is ever spent, and the whole
    # earned-autonomy argument is decoration.
    # With a store the streaks come from its shared ledger table, so every host earns and spends one streak.
    auto, gated = split_by_policy(
        proposals, cartridge=cartridge, ledger_path=args.ledger, provider_profile=provider_profile, store=store
    )

    if graph_name == _DECOMPOSE_GRAPH_NAME:
        _make_phase_dirs(
            Path(getattr(runner, "cwd", None) or Path.cwd()),
            _decompose_phase_dirs(proposals, cartridge, graph_args.get("initiative_id")),
        )

    auto_applied: list[dict[str, Any]] = []
    for item in auto:
        ok, detail = auto_apply(item, cartridge=cartridge, runner=runner)
        if ok:
            print(f"auto-applied {item['kind']} -> {item['target']}: {detail}")
            auto_applied.append(item)
        else:
            # Cleared by policy but nothing here can execute it. It goes to the
            # gate rather than being reported as done.
            print(f"auto-eligible but not executed ({detail}); sending to the gate", file=sys.stderr)
            gated.append(item)

    decisions, human_minutes = gate(gated, assume=args.assume)
    diffs = apply_decisions(decisions, cartridge=cartridge, runner=runner)

    # Settled straight after the arms, before the patch, manifest and ledger work, so the window in which a
    # task file exists and its row does not is as short as the run can make it. The closing line waits for the end.
    closing, missing = (
        _settle_decompose(
            proposals, diffs, auto_applied,
            cartridge=cartridge, graph_args=graph_args, runner=runner, runs_dir=Path(args.runs_dir),
        )
        if graph_name == _DECOMPOSE_GRAPH_NAME
        else ("", 0)
    )  # fmt: skip
    unfiled = 0
    if graph_name == _DECOMPOSE_GRAPH_NAME and not missing and approved_task_ids(proposals, diffs, auto_applied):
        root = Path(getattr(runner, "cwd", None) or Path.cwd())
        unfiled = _file_decompose_follow_ups(
            result,
            initiative=graph_args.get("initiative_id") or root.name,
            workspace=root,
            runs_dir=Path(args.runs_dir),
            date=args.date,
        )

    # A build patch is applied only after the gate approved the work it belongs
    # to. Skipped when --repo already ran the check arm above: the work is
    # already applied in a real worktree, and applying it again into the old
    # scratch dir would be redundant at best and misleading at worst.
    if not args.repo and args.graph == "lifecycle" and result.get("build", {}).get("patch"):
        approved = any(d["decision"] == "approved" for d in diffs)
        if approved:
            worktree = _lifecycle_worktree(args, cartridge, run_id)
            ok, detail = apply_patch(result["build"]["patch"], worktree)
            print(f"\npatch {'applied in' if ok else 'FAILED to apply in'} {worktree}")
            if not ok:
                print(f"  {detail}", file=sys.stderr)

    ts = datetime.now(UTC).isoformat()
    manifest = build_manifest(
        run_id=run_id,
        ts=ts,
        principal=graph_name,
        cartridge=cartridge,
        provider_profile=provider_profile,
        proposals=proposals,
        gate_diffs=diffs,
        human_minutes=human_minutes,
        totals={**result.get("totals", {}), "auto_applied": len(auto_applied), "gated": len(gated)},
    )
    append_ledger(manifest, ledger_path=args.ledger)
    if store is not None:
        _record_run_to_store(store, manifest, diffs, graph_name=graph_name, ledger_path=args.ledger)

    if result_out:
        Path(result_out).parent.mkdir(parents=True, exist_ok=True)
        Path(result_out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    # And then what the run itself established, on the same clock as the
    # manifest. This lands AFTER the manifest is recorded because it is a post-hoc verdict on
    # a run already recorded, not a second opinion on the gate.
    _observe_trap_failures(
        result,
        graph_name=graph_name,
        ts=ts,
        cartridge=cartridge,
        provider_profile=provider_profile,
        ledger_path=args.ledger,
    )

    # A decompose run's whole point is the task item_create it proposes. An
    # approved item_create that never actually landed (the arm refused, or
    # nothing executed it) means the intake was never decomposed, so the run
    # ends failed here rather than reporting the gate's approval as success.
    if graph_name == _DECOMPOSE_GRAPH_NAME:
        print(closing, file=sys.stderr if missing else sys.stdout)
        reason = unexecuted_item_create_reason(diffs)
        if reason is not None:
            print(f"decompose failed: no task item_create was executed: {reason}", file=sys.stderr)
            return 1
        if missing or unfiled:
            return 1

    print(f"\nrecorded {run_id}: {len(auto_applied)} auto-applied, {len(diffs)} gated decision(s), {len(proposals)} proposal(s)")
    print(f"  manifest: recorded in the run store under {run_id}")
    if result_out:
        print(f"  result  : {result_out}")
    print(f"  ledger  : {args.ledger}")
    return 0
