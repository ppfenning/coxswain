"""The harness command line: the parser, `main`, and what a run reports when it ends."""

from __future__ import annotations

import argparse
import contextlib
import copy
import os
import signal
import sys
import threading
from datetime import date as date_type
from pathlib import Path
from typing import Any

from harness.cli_launch import _launch, _stop_children_until_quiet, _terminate_children
from harness.cli_run_graph import DEFAULT_POOL_KEEP
from harness.decompose_ports import DecomposePorts
from harness.registry import GraphSpec, discover
from harness.termination import mark_terminating

REPO_ROOT = Path(__file__).resolve().parent.parent
# The cartridges component beside this one in the coxswain checkout: src/graphs -> src/cartridges.
CARTRIDGES_ROOT = REPO_ROOT.parent / "cartridges"


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


def _build_parser(specs: dict[str, GraphSpec]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shell.py",
        description="Resolve a cartridge, run a graph, gate its proposals, record the run.",
    )
    parser.add_argument(
        "graph",
        choices=sorted([*specs, "phase", "epic", "phase-lane", "rescue", "sweep"]),
        help=(
            "which graph to run ('phase' drives the lifecycle graph over one phase; "
            "'epic' drives a whole initiative, phase by phase, gating each one; "
            "'phase-lane' builds one phase a worker claimed, its tasks in parallel, each pushed as it rolls up; "
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
        help="epic: run only the named phase, repeatable; the phases' parents are taken as complete from the work store; "
        "phase-lane: the claimed phase",
    )
    parser.add_argument("--lane-worker", help="phase-lane: the worker whose phase_queue claim this lane runs under")
    parser.add_argument("--claim-epoch", type=int, help="phase-lane: the claim's epoch, which every write is fenced on")
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


def _non_negative(text: str) -> int:
    """argparse type: a pool keep limit of 0 or more; a negative would trim every idle worktree."""
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


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


def main(argv: list[str] | None = None, *, ports: DecomposePorts | None = None) -> int:
    """`ports` are the tools' store and route calls a decompose makes; `shell.py`, the composition root, passes them."""
    # Python's default SIGTERM action is to die without running any `finally`,
    # which would leave the run's `ended_at` unset. Only the main thread may
    # install a handler; the previous one is put back on every way out.
    if threading.current_thread() is not threading.main_thread():
        return _main(argv, ports)
    previous = signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        return _main(argv, ports)
    except SystemExit as exc:
        # `ended_at` is already stamped; interpreter exit would otherwise wait on fan-out workers.
        if exc.code == 143:
            _stop_children_until_quiet()
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def _refuse_phase_outside_epic(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """`--phase` selects phases of an initiative, which only the epic driver has."""
    if args.graph not in ("epic", "phase-lane") and getattr(args, "phase", None):
        parser.error("--phase applies to the epic and phase-lane graphs only")


def _expand_repo(args: argparse.Namespace) -> argparse.Namespace:
    """A copy of `args` whose `repo`, when given, has a leading tilde expanded to the home directory."""
    if not args.repo:
        return args
    expanded = copy.copy(args)
    expanded.repo = str(Path(args.repo).expanduser())
    return expanded


def _main(argv: list[str] | None, ports: DecomposePorts | None) -> int:
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
        return _launch(specs, parser, args, report_out=report_out, ports=ports)
