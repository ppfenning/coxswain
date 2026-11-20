"""The harness command line, a run: its lease, store records, traces, worktrees and progress."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import yaml

import core
from core import workstore
from core.cartridge import CartridgeError
from harness import CORE_SCHEMA, bounded_proc, node_progress, run_lease, store_traces
from harness.check_gate import gate_commands
from harness.checks import repo_checks
from harness.cli_run_graph import (
    _COS_PRINCIPAL,
    _PHASE_PRINCIPAL,
    _initiative_cost_read,
    _lifecycle_worktree,
    _provider_profile_scope,
    _run_graph,
    read_provider_profile,
)
from harness.digest import build_digest
from harness.lane_workers import lane_workers_from
from harness.registry import GraphSpec
from harness.resolve import overlay_path, resolve_cartridge, role_skill_bodies
from harness.runners import build_runner, runner_name
from harness.store_dialect import default_url
from harness.store_graphs import derive_definition, register
from harness.store_lease import acquire, release
from harness.store_migrate import open_store
from harness.store_read import calls as node_calls
from harness.store_read import cost_by_model, run_summary
from harness.store_write import Store
from harness.traces_url import have_pyarrow, redact_url, resolve_traces_root
from harness.worktree import keep_worktree, remove_worktree
from runner.protocol import LimitStop


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


def initiative_of_run(run_id: str) -> str | None:
    """The initiative of a run id of the form <initiative>-N; None for any other form."""
    head, sep, tail = run_id.rpartition("-")
    return head if sep and head and tail.isdigit() else None


_LEASE_TTL = 120  # seconds; the heartbeat renews every 30


def profile_storage_url(profile: Mapping[str, Any], runs_dir: Path | str) -> str:
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
    provider = read_provider_profile(args.provider_profile)
    url = profile_storage_url(provider, args.runs_dir)
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
            # a scripted run's approvals are canned answers: the row says so, so they never pass for a model's
            "runner": runner_name(args.scripted, provider),
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


def _print_usage(store: Store, run_id: str, runner: Any, *, with_initiative: bool = True) -> None:
    """Print the run's totals from the store. A store that fails here warns; it never changes the run's exit.

    The initiative's cost line follows when the run id is <initiative>-N. The epic prints its own beside its summary.
    """
    try:
        summary = run_summary(store.conn, run_id)
        models = cost_by_model(store.conn, run_id)
    except Exception as exc:
        print(f"store: could not read the usage of {run_id}: {' '.join(str(exc).split())}", file=sys.stderr)
        return
    line = _usage_line(summary, models)
    if line is not None:
        print(line)
    cost_line = _initiative_cost_read(store, initiative_of_run(run_id)) if with_initiative else None
    if cost_line is not None:
        print(cost_line)
    note = _undercount_note(run_id, summary["calls"] if summary else 0, len(getattr(runner, "calls", None) or []))
    if note is not None:
        print(note, file=sys.stderr)


def _read_overlay(repo: str | None) -> Any | None:
    """Parsed `<repo>/.agent/cartridge.yaml`, or None when repo or the file is absent."""
    if not repo or not Path(overlay_path(repo)).is_file():
        return None
    return yaml.safe_load(Path(overlay_path(repo)).read_text(encoding="utf-8"))


def pgrep_pids(text: str) -> list[int]:
    """The pids in `pgrep` output, one per line; blank and non-numeric lines are skipped."""
    return [int(line) for line in map(str.strip, text.splitlines()) if line.isdigit()]


def _pgrep_children(pid: int) -> str | None:
    """`pgrep -P <pid>` output; "" when exit 1 says no children, None when pgrep is missing or fails."""
    try:
        done = bounded_proc.completed(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False)
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


def _terminated(exc: BaseException) -> bool:
    """True for the exit `_exit_on_sigterm` raises."""
    return isinstance(exc, SystemExit) and exc.code == 143


def _stop_children_until_quiet(deadline_s: float = 30.0, poll_s: float = 0.1) -> None:
    """Keep stopping children until no worker thread is left, so a node retry cannot outlive the run."""
    end = time.monotonic() + deadline_s
    while _workers_alive() and time.monotonic() < end:
        _terminate_children()
        time.sleep(poll_s)


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
            profile_storage_url(read_provider_profile(args.provider_profile), args.runs_dir),
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
        _print_usage(store, run_id, runner, with_initiative=args.graph != "epic")
        close = getattr(runner, "close", None)
        if callable(close):
            close()
        # Compaction only reads the store and moves files, so it never changes the exit code.
        try:
            profile = read_provider_profile(args.provider_profile)
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
    from harness.epic import Ctx, mirror_read, rescue_task, resolve_default_ref, work_state_of

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
        work_state = work_state_of(read_provider_profile(args.provider_profile))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1, "failed"

    # The default branch, read as `run_epic` reads it, so a rescue checks against the ground an epic run would.
    repo = Path(args.repo)
    try:
        agent_checks = (repo / ".agent-checks").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        agent_checks = ""
    ctx = Ctx(
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
    mirror_read(ctx, items)
    mark = store.conn.dialect.placeholder
    stored = store.conn.query_all(f"SELECT run_id, ts, cause FROM attempts WHERE task_id = {mark}", (args.task,))
    try:
        report, status = rescue_task(ctx, _with_stored_causes(items[0], stored), phase=str(found["phase"])), "ok"
    except LimitStop as exc:
        report, status = {"status": "stopped", "why": exc.detail}, "stopped"
    print(json.dumps(report, sort_keys=True), file=report_out)
    return 0, status
