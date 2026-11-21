"""`cox lander`: run the landing worker, look at its queue, and write its service unit.

The lander is its own loop beside the chair. `once` makes one pass, `run` keeps making them, `status` shows every
landing record and queue item with how long it has waited, and `service` writes the systemd user unit. The handlers
are thin: the logic is in `lander.py`, the effects in `lander_ports.py`.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from agent_tools import lander, lander_ports, unit_env
from core import landing_record as lr
from core.jsontypes import is_mapping

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
        *([unit_env.environment_file_line(environment_file)] if environment_file is not None else []),
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


class _InitiativeStatus(TypedDict):
    initiative: str
    state: str
    branch: str | None
    parent: str | None
    phases: dict[str, str]
    pr: str | None
    held: object


class _QueueStatus(TypedDict):
    id: object
    kind: object
    state: object
    waiting: str
    attempts: object
    claimed_by: object
    last_error: object
    reason: object


class _UnreadableStatus(TypedDict):
    initiative: str
    why: str


class _Status(TypedDict):
    initiatives: list[_InitiativeStatus]
    unreadable: list[_UnreadableStatus]
    queue: list[_QueueStatus]


def status_rows(records: Sequence[lr.LandingRecord], items: Sequence[lander.Row], tasks: Mapping[str, Sequence[lander.Row]],
                now: str, unreadable: Sequence[lr.Unreadable] = ()) -> _Status:
    """Pure. What `cox lander status` shows: each live initiative's derived state and phases, each landing-record row
    that does not read (`core.landing_record.Unreadable`) with why, and each open queue item."""
    initiatives: list[_InitiativeStatus] = []
    for record in records:
        initiative = lander.record_text(record, "initiative")
        rows = list(tasks.get(initiative) or [])
        state = lr.initiative_state(record, rows) if rows else str(record.get("state") or "")
        if state == "landed":
            continue
        held = record.get("held")
        initiatives.append({
            "initiative": initiative, "state": state, "branch": record.get("branch"), "parent": record.get("parent"),
            "phases": lr.phase_states(record, rows) if rows else {},
            "pr": (record.get("pr") or {}).get("url"),
            "held": (held.get("reason") if is_mapping(held) else None) or None,
        })
    queue: list[_QueueStatus] = [
        {"id": i["id"], "kind": i["kind"], "state": i["state"], "waiting": age(str(i.get("queued_at") or ""), now),
         "attempts": i.get("attempts"), "claimed_by": i.get("claimed_by") or None, "last_error": i.get("last_error") or None,
         "reason": i.get("reason") or None}
        for i in items if i["state"] in ("queued", "claimed", "failed")
    ]
    return {"initiatives": initiatives, "unreadable": [{"initiative": u.initiative, "why": u.why} for u in unreadable],
            "queue": queue}


def status_lines(status: _Status) -> list[str]:
    """Pure. The same, as lines for a terminal."""
    lines: list[str] = []
    for row in status["initiatives"]:
        phases = ", ".join(f"{p} {s}" for p, s in row["phases"].items()) or "no tasks read"
        pr = f"  {row['pr']}" if row.get("pr") else ""
        lines.append(f"{row['initiative']}: {row['state']} on {row['branch']} -> {row['parent']}{pr}")
        lines.append(f"  phases: {phases}")
        if row.get("held"):
            lines.append(f"  held: {row['held']}")
    if not status["initiatives"]:
        lines.append("no initiative is in flight")
    for row in status["unreadable"]:
        lines.append(f"{row['initiative']}: landing record cannot be read, left as it is: {row['why']}")
    lines.append("")
    for item in status["queue"]:
        holder = f", held by {item['claimed_by']}" if item["state"] == "claimed" and item.get("claimed_by") else ""
        error = f": {item['last_error']}" if item.get("last_error") else ""
        lines.append(f"{item['id']}: {item['state']}, waiting {item['waiting']}, {item['attempts']} attempt(s){holder}{error}")
    if not status["queue"]:
        lines.append("the land queue is empty")
    return lines


def _workspace(a: argparse.Namespace) -> tuple[Path, dict[str, object]] | None:
    from agent_tools import cli

    profile: dict[str, object] | None = cli._courier_profile(a)
    return None if profile is None else (Path(str(profile["workspace_dir"])).expanduser(), profile)


def _caps(profile: Mapping[str, object]) -> dict[str, int]:
    from agent_tools import cli
    from core.landing import resolve_land_batch, resolve_land_cap, resolve_phase_cap

    cartridge: dict[str, object]
    cartridge, _ = cli._team_cartridge(dict(profile))
    return {"phase_cap": resolve_phase_cap(cartridge), "land_cap": resolve_land_cap(cartridge),
            "land_batch_size": resolve_land_batch(cartridge)}


class Lanes:
    """Runs each repository's lane of a pass on a thread of its own, with ports (and a store connection) of its own.

    With `wait` (`cox lander once`) a pass returns when every lane has. Without it (`cox lander run`) a lane runs on
    while the next passes start: a repository whose lane is still busy is left out of them until it is done, and
    every other repository goes on landing."""

    def __init__(self, open_ports: Callable[[], lander.Ports], caps: Callable[[], Mapping[str, int]], *, wait: bool,
                 log: Callable[[str], None] = print) -> None:
        self._open, self._caps, self._wait, self._log = open_ports, caps, wait, log
        self._pool = ThreadPoolExecutor(thread_name_prefix="lander-lane")
        self._busy: dict[str, Future[list[lander.Outcome]]] = {}

    def __call__(self, lanes: Sequence[lander.Lane]) -> list[lander.Outcome]:
        started: list[Future[list[lander.Outcome]]] = []
        for lane in lanes:
            running = self._busy.get(lane.repo)
            if running is not None and not running.done():
                continue
            self._busy[lane.repo] = self._pool.submit(self._work, lane)
            started.append(self._busy[lane.repo])
        return [outcome for future in started for outcome in future.result()] if self._wait else []

    def _work(self, lane: lander.Lane) -> list[lander.Outcome]:
        try:
            ports = self._open()
        except Exception as exc:
            self._log(f"lander: {lane.repo}: no store connection for its lane: {type(exc).__name__}: {exc}")
            return []
        try:
            return lander.run_lane(ports, lane, **self._caps())
        except Exception as exc:  # one lane's failure must not end the others, nor the loop
            self._log(f"lander: {lane.repo}: lane failed: {type(exc).__name__}: {exc}")
            return []
        finally:
            ports.conn.close()

    def close(self) -> None:
        self._pool.shutdown(wait=self._wait, cancel_futures=True)


def cmd_once(a: argparse.Namespace) -> int:
    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    lanes = Lanes(lambda: lander_ports.build(ws, profile, worker=ports.worker), lambda: _caps(profile), wait=True)
    try:
        outcomes = lander.run_once(ports, run_lanes=lanes)
    finally:
        lanes.close()
        ports.conn.close()
    if not outcomes:
        print("lander: nothing to do")
    return 1 if any(o.status == "failed" for o in outcomes) else 0


def cmd_run(a: argparse.Namespace) -> int:
    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    print(f"lander: {ports.worker} on {ws}, every {a.interval:g}s")
    lanes = Lanes(lambda: lander_ports.build(ws, profile, worker=ports.worker), lambda: _caps(profile), wait=False)
    told: str | None = None
    try:
        while True:
            failure: str | None = None
            try:
                lander.run_once(ports, run_lanes=lanes)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # one bad pass must not end the loop: the next pass reads everything again
                failure = f"pass failed: {type(exc).__name__}: {exc}"
                print(f"lander: {failure}")
            told = tell_pass_failure(ports, told, failure)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        lanes.close()
        ports.conn.close()


def tell_pass_failure(ports: lander.Ports, told: str | None, failure: str | None) -> str | None:
    """Tell the chair of a failed pass once per distinct failure (`lander.pass_failure_notice`): what is told now."""
    told, reason = lander.pass_failure_notice(told, failure)
    if reason is not None:
        try:
            ports.notify("needs_chair", {"initiative": "", "reason": f"the lander's {reason}; it tries again every pass"})
        except Exception as exc:  # a bus that cannot be written is said here, and told again next pass
            print(f"lander: the chair was not told: {type(exc).__name__}: {exc}")
            return None
    return told


def cmd_status(a: argparse.Namespace) -> int:
    from harness import landing_store

    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    try:
        table = landing_store.list_records(ports.conn)
        status = status_rows(table.records, landing_store.list_items(ports.conn),
                             {lander.record_text(r, "initiative"): ports.tasks(lander.record_text(r, "initiative")) for r in table.records},
                             ports.now(), table.unreadable)
    finally:
        ports.conn.close()
    print(json.dumps(status, indent=2) if a.json else "\n".join(status_lines(status)))
    return 0


def cmd_adopt(a: argparse.Namespace) -> int:
    """Dry run unless `--apply`: prints, per initiative with no landing record, what adopting it would do."""
    from agent_tools import lander_adopt, work_state
    from agent_tools.store_url import read_provider_profile
    from harness import landing_store

    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    try:
        named = list(a.initiative) or lander_ports.initiatives_in(ws)
        provider: Mapping[str, object] = read_provider_profile(str(profile["provider_profile"])) if profile.get("provider_profile") else {}
        send_back = lander_ports.send_back(ws, ports.conn, ports.worker, work_state.work_state_mode(provider))
        stopped = False
        for line in lander_adopt.left_alone(named, landing_store.list_records(ports.conn)):
            print(line)
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


def cmd_release(a: argparse.Namespace) -> int:
    """Releases one initiative's merge hold for the next pass; exit 1 when there is none to release."""
    found = _workspace(a)
    if found is None:
        return 2
    ws, profile = found
    ports = lander_ports.build(ws, profile)
    try:
        released, said = lander.release_merge_hold(ports, a.initiative)
    finally:
        ports.conn.close()
    print(f"lander: {said}" if not released else said)
    return 0 if released else 1


def cmd_backlog(a: argparse.Namespace) -> int:
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
            found_state = next((t["state"] for t in ports.tasks(initiative) if t["id"] == task), None)
            return None if found_state is None else str(found_state)

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


def cmd_service(a: argparse.Namespace) -> int:
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

