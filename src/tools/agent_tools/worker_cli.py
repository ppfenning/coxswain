"""`cox worker`: run the phase worker on this machine, or look at the phase queue and the workers.

`run` claims and runs phases until interrupted; `once` makes one claim pass and runs what it claimed to its end;
`status` prints the queue and the registry; `checkout` clones a repository through this machine's mirror
(`harness.worktree.clone_worktree`), so a machine joining as a worker gets the checkout its `repo:` token needs.
The handlers are thin: the loop is `worker.py`, the effects `worker_ports.py`, the store `harness.store_phases`.
There is no service unit yet: the CLI is the whole surface.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from agent_tools import worker, worker_ports
from harness import phase_pick, repo_cache, worktree


def claim_of(row: Mapping[str, object], now: str) -> dict[str, object] | None:
    """Pure. A claimed row's claim as of the store's `now`: who holds it, at which epoch, until when, and the run it
    started; and whether it lapsed (nobody renewed it and nobody has claimed it since), with how many seconds ago and
    whether the chair has marked its run lost (`phase_pick.lost_reason`). None for a row nobody claims."""
    if row.get("state") != "claimed":
        return None
    epoch = row.get("claim_epoch")
    holder, until = str(row.get("claimed_by") or ""), str(row.get("lease_until") or "")
    lapsed = not phase_pick.fenced(row, holder, epoch if isinstance(epoch, int) else -1, now)
    try:
        ago: int | None = phase_pick.seconds(until, now) if lapsed else None
    except ValueError:
        ago = None
    marked = lapsed and str(row.get("reason") or "") == phase_pick.lost_reason(row)
    return {"holder": holder, "epoch": epoch, "lease_until": until, "run_id": str(row.get("run_id") or ""),
            "lapsed": lapsed, "lapsed_s": ago, "marked_lost": marked}


def _held(row: Mapping[str, object], now: str) -> str:
    claim = claim_of(row, now) or {}
    if not claim.get("lapsed"):
        return f"held by {claim.get('holder')} until {claim.get('lease_until')}"
    ago = f", {claim['lapsed_s']}s ago" if isinstance(claim.get("lapsed_s"), int) else ""
    run = f"; run {claim['run_id']} lost" if claim.get("run_id") else ""
    marked = " (the chair marked it)" if claim.get("marked_lost") else ""
    was = f"was {claim.get('holder')}'s, epoch {claim.get('epoch')}"
    return f"lapsed at {claim.get('lease_until')}{ago} ({was}){run}{marked}, claimable"


def status_lines(phases: Sequence[Mapping[str, object]], workers: Sequence[Mapping[str, object]], now: str) -> list[str]:
    """Pure. One line per queued phase, then one per worker, as of the store's `now`."""
    lines: list[str] = []
    for row in phases:
        state = str(row.get("state"))
        raw = row.get("requires_json")
        requires = ", ".join(str(t) for t in cast("list[object]", raw)) if isinstance(raw, list) else ""
        where = {
            "claimed": _held(row, now),
            "finished": f"{row.get('outcome')}: {row.get('reason')}",
            "ready": f"claimable from {row.get('ready_at')}" if str(row.get("ready_at") or "") > now else "claimable",
        }.get(state, "")
        lines.append(
            f"{row.get('initiative')}/{row.get('phase')}: {state}, epoch {row.get('claim_epoch')}, "
            f"{row.get('attempts')} attempt(s), needs [{requires}]" + (f"; {where}" if where else "")
            + (f"; run {row.get('run_id')}" if row.get("run_id") and not (claim_of(row, now) or {}).get("lapsed") else "")
        )
    if not phases:
        lines.append("the phase queue is empty")
    lines.append("")
    for row in workers:
        caps = row.get("capabilities_json")
        lines.append(f"worker {row.get('name')}: {row.get('state')}, {row.get('lanes')} lane(s), "
                     f"[{', '.join(str(c) for c in caps) if isinstance(caps, list) else ''}], beat {row.get('beat_at')}")
    if not workers:
        lines.append("no worker has registered")
    return lines


def _workspace(a: argparse.Namespace) -> Path | None:
    from agent_tools import cli

    profile: dict[str, object] | None = cli._courier_profile(a)
    return None if profile is None else Path(str(profile["workspace_dir"])).expanduser()


def _version() -> str:
    from agent_tools.version_info import package_version

    try:
        return package_version()
    except Exception:  # a version that cannot be read names nothing; the worker runs all the same
        return ""


def _run(a: argparse.Namespace, *, once: bool) -> int:
    ws = _workspace(a)
    if ws is None:
        return 2
    bad = phase_pick.bad_tokens(a.capability or [])
    if bad:
        print(f"worker: not a capability token (lowercase `name` or `key:value`): {', '.join(bad)}")
        return 2
    given = getattr(a, "lease_s", None)
    lease_s = given if isinstance(given, int) else phase_pick.LEASE_S
    if problem := phase_pick.lease_problem(lease_s):
        print(f"worker: --lease-s {lease_s}: {problem}; a lease runs from {phase_pick.MIN_LEASE_S}s to "
              f"{phase_pick.MAX_LEASE_S}s")
        return 2
    name = a.name or socket.gethostname()
    ports, store = worker_ports.build(ws, name=name, declared=list(a.capability or []), lanes=a.lanes, version=_version(),
                                      lease_s=lease_s)
    if not once:
        print(f"worker: {name} on {ws}; heartbeat every {ports.timings.heartbeat_s}s, lease {lease_s}s", flush=True)
    # SIGTERM (a service manager's stop) is handled as SIGINT is: stop every run, confirm it is gone, give claims back.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        return worker.run(ports, once=once)
    finally:
        store.close()


def cmd_run(a: argparse.Namespace) -> int:
    return _run(a, once=False)


def cmd_once(a: argparse.Namespace) -> int:
    return _run(a, once=True)


def cmd_status(a: argparse.Namespace) -> int:
    ws = _workspace(a)
    if ws is None:
        return 2
    store = worker_ports.Store(ws / "runs")
    try:
        phases, workers, now = store.call(worker_ports.status)
    finally:
        store.close()
    if a.json:
        shown = [{**row, "claim": claim_of(row, now)} for row in phases]
        print(json.dumps({"now": now, "phases": shown, "workers": workers}, indent=2, default=str))
    else:
        print("\n".join(status_lines(phases, workers, now)))
    return 0


def cmd_checkout(a: argparse.Namespace) -> int:
    """Clone `a.url` into `a.dest` through the mirror under the worker's cache directory, and say what it cost."""
    url, dest = str(a.url), Path(str(a.dest)).expanduser()
    root = repo_cache.cache_root(os.environ, os.path.expanduser("~"))
    cloned = worktree.clone_worktree(url, dest, cache_root=root, branch=a.branch, base=a.base)
    if a.json:
        print(json.dumps({**cloned._asdict(), "url": url, "dest": str(dest), "cache_root": str(root)}, indent=2))
    else:
        print(cloned.trace(url))
        if not cloned.ok:
            print(f"worker: checkout failed: {cloned.detail}")
    return 0 if cloned.ok else 1
