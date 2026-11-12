"""`cox lander`: run the landing worker, look at its queue, and write its service unit.

The lander is its own loop beside the chair. `once` makes one pass, `run` keeps making them, `status` shows every
landing record and queue item with how long it has waited, and `service` writes the systemd user unit. The handlers
are thin: the logic is in `lander.py`, the effects in `lander_ports.py`.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import lander, lander_ports

RUN_INTERVAL_S = 30.0  # the poll floor: an event wakes nothing yet, so this is how soon a built phase is merged
UNIT_NAME = "coxswain-lander.service"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def unit_path(home: Path) -> Path:
    return home / ".config/systemd/user" / UNIT_NAME


def unit_text(cox: str, workspace_dir: str, interval: int, profile: str | None = None, path: str | None = None,
              environment_file: str | None = None) -> str:
    """Pure. The lander's user unit, the chair unit's sibling. `EnvironmentFile=-` keeps a missing file from failing it."""
    profile_args = f" --profile {profile}" if profile is not None else ""
    lines = [
        "[Unit]",
        "Description=Coxswain lander: merges built phases and lands whole initiatives",
        "",
        "[Service]",
        f"ExecStart={cox} lander run --interval {interval}{profile_args}",
        f"WorkingDirectory={workspace_dir}",
        *([f"Environment=PATH={path}"] if path is not None else []),
        *([f"EnvironmentFile=-{environment_file}"] if environment_file is not None else []),
        "Restart=on-failure",
        "RestartSec=30",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"


def systemctl_lines() -> list[str]:
    return ["systemctl --user daemon-reload", f"systemctl --user enable --now {UNIT_NAME}"]


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)


def age(since: str, now: str) -> str:
    """Pure. `3m`, `2h05m` or `4d02h` between two stamps; `?` when either does not parse."""
    try:
        seconds = int((_parse(now) - _parse(since)).total_seconds())
    except (TypeError, ValueError):
        return "?"
    seconds = max(seconds, 0)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02d}h"


def status_rows(records: Sequence[Mapping[str, Any]], items: Sequence[Mapping[str, Any]], tasks: Mapping[str, Sequence[Mapping[str, Any]]],
                now: str) -> dict[str, Any]:
    """Pure. What `cox lander status` shows: each live initiative's derived state and phases, and each open queue item."""
    from core import landing_record as lr

    initiatives = []
    for record in records:
        rows = list(tasks.get(str(record["initiative"])) or [])
        state = lr.initiative_state(record, rows) if rows else str(record.get("state") or "")
        if state == "landed":
            continue
        initiatives.append({
            "initiative": record["initiative"], "state": state, "branch": record.get("branch"), "parent": record.get("parent"),
            "phases": lr.phase_states(record, rows) if rows else {},
            "pr": (record.get("pr") or {}).get("url"),
        })
    queue = [
        {"id": i["id"], "kind": i["kind"], "state": i["state"], "waiting": age(str(i.get("queued_at") or ""), now),
         "attempts": i.get("attempts"), "claimed_by": i.get("claimed_by") or None, "last_error": i.get("last_error") or None,
         "reason": i.get("reason") or None}
        for i in items if i["state"] in ("queued", "claimed", "failed")
    ]
    return {"initiatives": initiatives, "queue": queue}


def status_lines(status: Mapping[str, Any]) -> list[str]:
    """Pure. The same, as lines for a terminal."""
    lines = []
    for row in status["initiatives"]:
        phases = ", ".join(f"{p} {s}" for p, s in row["phases"].items()) or "no tasks read"
        pr = f"  {row['pr']}" if row.get("pr") else ""
        lines.append(f"{row['initiative']}: {row['state']} on {row['branch']} -> {row['parent']}{pr}")
        lines.append(f"  phases: {phases}")
    if not status["initiatives"]:
        lines.append("no initiative is in flight")
    lines.append("")
    for item in status["queue"]:
        holder = f", held by {item['claimed_by']}" if item["state"] == "claimed" and item.get("claimed_by") else ""
        error = f": {item['last_error']}" if item.get("last_error") else ""
        lines.append(f"{item['id']}: {item['state']}, waiting {item['waiting']}, {item['attempts']} attempt(s){holder}{error}")
    if not status["queue"]:
        lines.append("the land queue is empty")
    return lines


def _workspace(a: argparse.Namespace) -> tuple[Path, dict] | None:
    from agent_tools import cli

    profile = cli._courier_profile(a)
    return None if profile is None else (Path(profile["workspace_dir"]).expanduser(), profile)


def _caps(profile: Mapping[str, Any]) -> dict[str, int]:
    from agent_tools import cli
    from core.landing import resolve_land_batch, resolve_land_cap, resolve_phase_cap

    cartridge, _ = cli._team_cartridge(dict(profile))
    return {"phase_cap": resolve_phase_cap(cartridge), "land_cap": resolve_land_cap(cartridge),
            "land_batch_size": resolve_land_batch(cartridge)}


def _once(a: argparse.Namespace) -> int:
    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    try:
        outcomes = lander.run_once(ports, **_caps(profile))
    finally:
        ports.conn.close()
    if not outcomes:
        print("lander: nothing to do")
    return 1 if any(o.status == "failed" for o in outcomes) else 0


def _run(a: argparse.Namespace) -> int:
    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    print(f"lander: {ports.worker} on {ws}, every {a.interval:g}s")
    try:
        while True:
            try:
                lander.run_once(ports, **_caps(profile))
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # one bad pass must not end the loop: the next pass reads everything again
                print(f"lander: pass failed: {type(exc).__name__}: {exc}")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        ports.conn.close()


def _status(a: argparse.Namespace) -> int:
    from harness import landing_store

    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    try:
        records = landing_store.list_records(ports.conn)
        status = status_rows(records, landing_store.list_items(ports.conn),
                             {str(r["initiative"]): ports.tasks(str(r["initiative"])) for r in records}, ports.now())
    finally:
        ports.conn.close()
    print(json.dumps(status, indent=2) if a.json else "\n".join(status_lines(status)))
    return 0


def _adopt(a: argparse.Namespace) -> int:
    """Dry run unless `--apply`: prints, per initiative with no landing record, what adopting it would do."""
    from agent_tools import lander_adopt, work_state
    from agent_tools.store_url import read_provider_profile

    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    try:
        named = list(a.initiative) or lander_ports.initiatives_in(ws)
        provider = read_provider_profile(profile["provider_profile"]) if profile.get("provider_profile") else {}
        send_back = lander_ports.send_back(ws, ports.conn, ports.worker, work_state.work_state_mode(provider))
        stopped = False
        for initiative in lander_adopt.candidates(ports, named):
            plan = lander_adopt.plan(ports, initiative)
            lines = lander_adopt.apply(ports, plan, send_back) if a.apply else plan.lines()
            stopped = stopped or any(": stopped: " in line for line in lines)
            print("\n".join(lines))
        if not a.apply:
            print("dry run: nothing was changed; run again with --apply to adopt")
    finally:
        ports.conn.close()
    return 1 if stopped else 0


def _backlog(a: argparse.Namespace) -> int:
    """Dry run unless `--apply`; with `--apply` it refuses until the forge says the pull request merged."""
    from agent_tools import lander_backlog

    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    try:
        manifest = lander_backlog.read_manifest(Path(a.manifest))
    except ValueError as exc:
        print(f"lander: {exc}")
        return 2
    ports = lander_ports.build(ws, profile)
    try:
        def state_of(initiative: str, task: str) -> str | None:
            return next((t["state"] for t in ports.tasks(initiative) if t["id"] == task), None)

        steps = lander_backlog.plan(manifest, state_of)
        if not a.apply:
            print("\n".join([*lander_backlog.lines(steps), "dry run: nothing was changed; run again with --apply once the pull request has merged"]))
            return 0
        state = ports.forge.pr_state(a.pr)
        if state.get("state") != "merged":
            print(f"lander: refusing: {a.pr} is {state.get('state') or 'unknown'}, not merged; its tasks are not on the parent yet")
            return 2
        print("\n".join(lander_backlog.apply(steps, ports.mark_done, a.pr)))
    finally:
        ports.conn.close()
    return 0


def _service(a: argparse.Namespace) -> int:
    """Writes or shows the unit. Never runs systemctl: it prints the two lines to run."""
    from agent_tools import cli

    path = unit_path(Path.home())
    if a.install:
        found = _workspace(a)
        if found is None:
            return 2
        ws, _profile = found
        text = cli._lander_unit_text(a, ws)
        if text is None:
            return 2
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(path)
        print("\n".join(systemctl_lines()))
        return 0
    text = cli._read_text_or_none(path)
    print(f"no unit at {path}" if text is None else text, end="" if text else "\n")
    return 0

