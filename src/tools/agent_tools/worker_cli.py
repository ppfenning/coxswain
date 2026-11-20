"""`cox worker`: run the phase worker on this machine, or look at the phase queue and the workers.

`run` claims and runs phases until interrupted; `once` makes one claim pass and runs what it claimed to its end;
`status` prints the queue and the registry. The handlers are thin: the loop is `worker.py`, the effects
`worker_ports.py`, the store `harness.store_phases`. There is no service unit yet: the CLI is the whole surface.
"""

from __future__ import annotations

import argparse
import json
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from agent_tools import worker, worker_ports
from harness import phase_pick


def _held(row: Mapping[str, object], now: str) -> str:
    epoch = row.get("claim_epoch")
    worker_name = str(row.get("claimed_by") or "")
    live = phase_pick.fenced(row, worker_name, epoch if isinstance(epoch, int) else -1, now)
    return f"held by {worker_name} until {row.get('lease_until')}" if live else f"lapsed (was {worker_name}), claimable"


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
            + (f"; run {row.get('run_id')}" if row.get("run_id") else "")
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
    name = a.name or socket.gethostname()
    ports, store = worker_ports.build(ws, name=name, declared=list(a.capability or []), lanes=a.lanes, version=_version())
    if not once:
        print(f"worker: {name} on {ws}; heartbeat every {phase_pick.HEARTBEAT_S}s, lease {phase_pick.LEASE_S}s")
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
        print(json.dumps({"now": now, "phases": phases, "workers": workers}, indent=2, default=str))
    else:
        print("\n".join(status_lines(phases, workers, now)))
    return 0
