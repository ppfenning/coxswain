"""`cox route chair`: the chair lease, taking, extending and releasing it, and the chair chat."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_courier import _courier_workspace, _parse_fields
from agent_tools.cli_land import _leader_identity, _leader_pid_alive, _leader_pid_choice, _leader_read_or_refuse
from agent_tools.cli_lazy import (
    chair,
    chair_chat,
    chair_holder,
    chair_resolve,
    chair_run,
    chair_service,
    courier,
    leader_chat,
    route,
    run_store,
)
from agent_tools.cli_runs import _leader_heartbeat_minutes, _leader_runs_dir_or_refuse


def _leader_refuse_hours(hours: float | None) -> int | None:
    if hours is None or hours > 0:
        return None
    print(f"chair: --hours must be positive, got {hours}")
    return 2


def _leader_take_warning(pid: int, explicit_pid: int | None, env: Mapping[str, str], ppid: int, alive: Callable[[int], bool]) -> str | None:
    """Pure. The warning for a take that records the invoking shell's pid, else None.
    `pid` is the pid actually recorded, so a pid read back off a held lease counts as the shell's when it equals `ppid`."""
    if explicit_pid or pid != ppid or _leader_pid_choice(None, env, ppid, alive) == (ppid, False):
        return None
    return f"warning: recorded pid {pid} is the invoking shell's; the lease goes stale when it exits. Pass --pid with a long-lived loop's pid."


def _leader_refuse_dead_pid(explicit_pid: int | None) -> int | None:
    """Refuses only an explicit `--pid`; the `os.getppid()` default is alive by construction."""
    if explicit_pid is None or chair.pid_alive(explicit_pid):
        return None
    print(f"chair pid not alive: {explicit_pid}")
    return 2


def _print_if_stale(record: dict | None, state: str) -> None:
    if record is not None and state in ("stale", "crashed"):
        print(f"chair {state}: {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')}")


def _retaken_chair(record: dict, now_iso: str, model: str | None, session: str, pid: int, host: str, hours: float | None, now: datetime.datetime) -> tuple[dict, str]:
    """Pure. The record to write and the line to print when a take retakes a lease its own holder holds.
    A given `model` replaces the recorded one, none keeps it; given `hours` set `until` to `now` plus hours, none keeps the recorded `until`."""
    refreshed = {**record, "heartbeat_at": now_iso, **({"model": model} if model else {})}
    new_record = refreshed if hours is None else chair.extend(refreshed, now, hours)
    recorded = new_record.get("model")
    suffix = f" (model {recorded})" if isinstance(recorded, str) and recorded else ""
    until = "" if hours is None else f" until {new_record['until']}"
    return new_record, f"chair lease retaken: {session} (pid {pid}) on {host}{suffix}{until}"


def _read_key() -> str:
    """Edge. One keypress, no Enter. Without a terminal nothing confirms an attach or a takeover, so the answer is b, a non-chair session."""
    if not sys.stdin.isatty():
        return "b"
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _caller_holder(runs_dir: Path, explicit_label: str | None) -> tuple[str, int] | None:
    """Edge. The (label, pid) of the live lease this caller holds, or None; an explicit label opts out so a mismatch is still refused."""
    if explicit_label:
        return None
    try:
        record = chair.read(runs_dir)
    except (OSError, ValueError):
        return None
    host = socket.gethostname()
    state = chair.liveness(record, _leader_pid_alive(record), datetime.datetime.now(datetime.UTC), host, _leader_heartbeat_minutes())
    label = chair.resolve_holder_label(record, state, os.environ, host, None)
    return None if label is None else (label, record["pid"])


def _route_chair_take(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    held = _caller_holder(runs_dir, a.label)
    session = a.label or (held[0] if held else "chair")
    pid, host = _leader_identity(a.pid or (held[1] if held else None), os.environ)
    warning = _leader_take_warning(pid, a.pid, os.environ, os.getppid(), chair.pid_alive)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    hours = getattr(a, "hours", None)  # the bare launcher's namespace has none: its record carries no `until`
    refuse_rc = _leader_refuse_hours(hours)
    if refuse_rc is not None:
        return refuse_rc
    heartbeat_minutes = _leader_heartbeat_minutes()
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        now = datetime.datetime.now(datetime.UTC)
        alive = _leader_pid_alive(record)
        prior_state = chair.liveness(record, alive, now, host, heartbeat_minutes)
        _print_if_stale(record, prior_state)
        new_record, reason = chair.take(record, session, pid, host, now, heartbeat_minutes, alive, steal=a.steal, claude_session=getattr(a, "claude_session", None) or chair.claude_session_from_env(os.environ), hours=hours, model=getattr(a, "model", None), cwd=getattr(a, "cwd", None) or os.getcwd())
        if new_record is None and prior_state == "live" and not chair.expired(record, now) and chair.same_holder(record, session, pid, host):
            retaken, line = _retaken_chair(record, now.isoformat(), getattr(a, "model", None), session, pid, host, hours, now)
            refusal = chair.acquire_lease(runs_dir, session, pid, host, ttl=chair.lease_ttl_for(retaken, now), steal=a.steal)
            if refusal:
                print(refusal)
                return 2
            chair.write(runs_dir, retaken)
            print(line)
            if warning:
                print(warning)
            return 0
        if new_record is None:
            print(reason)
            return 2
        refusal = chair.acquire_lease(runs_dir, session, pid, host, ttl=chair.lease_ttl_for(new_record, now), steal=a.steal)
        if refusal:
            print(refusal)
            return 2
        chair.write(runs_dir, new_record)
        displaced = a.steal and prior_state == "live" and not chair.expired(record, now)
    if displaced:
        print(chair.displaced_line(record))
    print(f"chair taken: {new_record['session']} (pid {new_record['pid']}) on {new_record['host']}")
    if warning:
        print(warning)
    return 0


def _route_chair_extend(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    session = a.label or "chair"
    pid, host = _leader_identity(a.pid, os.environ)
    refuse_rc = _leader_refuse_dead_pid(a.pid) or _leader_refuse_hours(a.hours)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        if not chair.same_holder(record, session, pid, host):
            print(f"chair: not held by {session} (pid {pid}) on {host}")
            return 2
        new_record = chair.extend(record, datetime.datetime.now(datetime.UTC), a.hours)
        chair.write(runs_dir, new_record)
    print(f"chair extended until {new_record['until']}")
    return 0


def _route_chair_beat(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    held = _caller_holder(runs_dir, a.label)
    session = a.label or (held[0] if held else "chair")
    pid, host = _leader_identity(a.pid or (held[1] if held else None), os.environ)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        now = datetime.datetime.now(datetime.UTC)
        new_record, reason = chair.beat(record, session, pid, host, now, run_id=a.run, claude_session=chair.claude_session_from_env(os.environ))
        if new_record is None:
            print(reason)
            return 2
        lost = chair.renew_lease(runs_dir, session, pid, host, ttl=chair.lease_ttl_for(new_record, now))
        if lost:
            print(lost)
            return 2
        chair.write(runs_dir, new_record)
    print(f"chair heartbeat: {new_record['session']}")
    return 0


def _route_chair_release(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    held = _caller_holder(runs_dir, a.label)
    session = a.label or (held[0] if held else "chair")
    pid, host = _leader_identity(a.pid or (held[1] if held else None), os.environ)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        _new_record, reason = chair.release(record, session, pid, host)
        if reason:
            print(reason)
            return 2
        chair.release_lease(runs_dir, session, pid, host)
        chair.write(runs_dir, None)
    print(f"chair released: {record['session']}")
    return 0


def _route_chair_status(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    record, read_rc = _leader_read_or_refuse(runs_dir)
    if read_rc is not None:
        return read_rc
    now = datetime.datetime.now(datetime.UTC)
    alive = _leader_pid_alive(record)
    state = chair.liveness(record, alive, now, socket.gethostname(), _leader_heartbeat_minutes())
    if not a.json:
        _print_if_stale(record, state)
    if a.json:
        print(json.dumps({**(record or {}), "state": state}, indent=2))
    elif record is None:
        print("chair: none")
    else:
        left = chair.hours_left(record, now)
        window = "" if left is None else f" until {record['until']} ({'expired' if chair.expired(record, now) else f'{left:.1f}h left'})"
        print(f"chair: {record['session']} (pid {record['pid']}) on {record['host']} — {state}{window}")
    return 0


def _route_leader_status(a: argparse.Namespace) -> int:
    """The SessionStart line: one `chair:` line saying whether this session is the chair."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    record, read_rc = _leader_read_or_refuse(runs_dir)
    if read_rc is not None:
        return read_rc
    host = socket.gethostname()
    live = _live_loop_holder(runs_dir)
    caller_holds = chair.is_caller_holder(record, os.environ, host)
    if live is not None and not caller_holds:
        print(chair_resolve.held_line(live.label or "?", live.is_service))  # a live chair loop holds the chair: read only, the lease is not touched
        return 0
    # A record matched by session id alone can be a resumed session's, while the live holder is another process.
    # Only a caller holding the record by pid is that holder. The hook source is not read: every source decides the same.
    by_pid = record is not None and str(os.environ.get("CLAUDE_PID", "")) == str(record.get("pid"))
    decision = chair_resolve.session_start_decision(
        _launcher_session_takes(getattr(a, "profile", None)), record, live, caller_holds, caller_label=str(record.get("session")) if by_pid and record else None,
    )
    if live is not None and decision.action == "observer":
        print(decision.line)  # read only: the lease and chair.json are not touched
        return 0
    state = chair.liveness(record, _leader_pid_alive(record), datetime.datetime.now(datetime.UTC), host, _leader_heartbeat_minutes())
    with chair.locked(runs_dir):  # re-read under the lock: a beat or a takeover's release request may have landed since the read above
        fresh = chair.read(runs_dir)
        record = chair.adopt_session(fresh, state, os.environ, host)
        line = chair.session_line(record, state, os.environ, host)
        settled = chair.settle_resumed(record, state, os.environ, host)
        if settled is not fresh:
            chair.write(runs_dir, settled)
    print(line)
    return 0


def _route_chair_clear(a: argparse.Namespace) -> int:
    """Holds `chair.locked` across the whole read-decide-unlink sequence, same as
    take/beat/release, so a `take` landing mid-clear cannot be discarded silently."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        result = chair.clear(runs_dir, force=a.force)
    if result is None:
        print("chair: nothing cleared")
        return 0
    path, record = result
    print(f"chair cleared: {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')} [{path.name}]")
    return 0


def _route_chair_chat(a: argparse.Namespace) -> int:
    """`--read` prints the thread (or, with `--since`, only what's unread); otherwise
    appends one line, as the operator by default or, with `--as-leader`, only when this
    process's pid and host match the live lock's holder — a reply must never be shown
    from a session that is not the leader."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    path = leader_chat.chat_path(runs_dir)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if a.read:
        thread = leader_chat.read_thread(existing, limit=50)
        entries = leader_chat.unread(thread, a.since)
        if a.json:
            print(json.dumps(entries))
        else:
            for entry in entries:
                print(f"{entry.get('at', '?')} {entry.get('from', '?')}: {entry.get('text', '')}")
        return 0
    if a.text is None:
        print("chat: TEXT required unless --read")
        return 2
    sender = "operator"
    if a.as_leader:
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        pid, host = _leader_identity(None, os.environ)
        now = datetime.datetime.now(datetime.UTC)
        state = chair.liveness(record, _leader_pid_alive(record), now, host, _leader_heartbeat_minutes())
        if state != "live":
            print("chat: refusing --as-leader (no live lock held)")
            return 2
        if record.get("pid") != pid or record.get("host") != host:
            print(f"chat: refusing --as-leader (held by {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')})")
            return 2
        sender = record.get("session", "leader")
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.datetime.now(datetime.UTC).isoformat(), "from": sender, "text": a.text}
    path.write_text(leader_chat.append_line(existing, entry), encoding="utf-8")
    print(f"chat: {sender}: {a.text}")
    return 0


def _chat_state_path(workspace: Path) -> Path:
    return workspace / "chair_chat_state.json"


def _read_chat_doc(path: Path) -> tuple[dict, list[dict]]:
    """Edge. The directive state and the epics the chair last saw; a missing or torn file reads as empty."""
    try:
        loaded = json.loads(_read_text_or_none(path) or "{}")
    except json.JSONDecodeError:
        loaded = {}
    doc = loaded if isinstance(loaded, dict) else {}
    base = chair_chat.empty_state()
    saved = doc.get("state") if isinstance(doc.get("state"), dict) else {}
    epics = doc.get("epics") if isinstance(doc.get("epics"), list) else []
    return {**base, **{k: v for k, v in saved.items() if k in base}}, epics


def _write_chat_doc(path: Path, state: dict, epics: list[dict]) -> None:
    """Edge. Replace the file atomically; an unchanged document is not rewritten."""
    text = json.dumps({"state": state, "epics": epics}, default=sorted, sort_keys=True)
    if _read_text_or_none(path) == text:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _chat_deps(ws: Path) -> chair_run.ChatDeps:
    """Edge. The chat edges over the workspace's `courier.jsonl` and `chair_chat_state.json`; neither is the lease."""
    courier_path, chat_path = ws / "courier.jsonl", _chat_state_path(ws)
    return chair_run.ChatDeps(
        read=lambda: courier.entries(courier.read_text(courier_path)),  # no bus yet is an empty bus
        load=lambda: _read_chat_doc(chat_path)[0],
        save=lambda state, epics: _write_chat_doc(chat_path, state, epics),
        append=lambda entry: courier.append_file(courier_path, entry),
        ack=lambda message_id: courier.ack_file(courier_path, message_id, chair_run.CHAT_CONSUMER),
    )


def _converse_append(a: argparse.Namespace, body_kind: str, payload: dict[str, str]) -> tuple[Path, str] | None:
    """Edge. Append one chat entry to the workspace courier; the lease record is never opened. None after printing why."""
    workspace = _courier_workspace(a)
    if workspace is None:
        return None
    message_id = uuid.uuid4().hex
    entry = chair_chat.build_message(a.sender, body_kind, payload, message_id, chair_run.CHAT_REF)
    courier.append_file(workspace / "courier.jsonl", entry)
    return workspace, message_id


def _route_chair_converse_send(a: argparse.Namespace) -> int:
    fields = _parse_fields(a.args or [])
    if fields is None:
        print(f"converse: bad argument in {a.args}; expected key=value")
        return 2
    payload = {**fields, "kind": a.kind}
    reason = chair_chat.validate_directive(a.kind, payload)
    if reason is not None:
        print(f"converse: {reason}")
        return 2
    sent = _converse_append(a, "directive", payload)
    if sent is None:
        return 2
    print(f"converse: sent {a.kind} directive {sent[1]}; the chair applies it on its next tick")
    return 0


def _find_chat_answer(blob: str, question_id: str) -> dict | None:
    return next((e for e in courier.entries(blob) if e.get("event") == "chat_answer" and (e.get("fields") or {}).get("question") == question_id), None)


def _await_chat_answer(
    path: Path, question_id: str, wait: float, poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
) -> dict | None:
    """Edge. The answer entry for `question_id`, polled until `wait` seconds pass; None when none came."""
    deadline = monotonic() + wait
    while True:
        found = _find_chat_answer(courier.read_text(path), question_id)
        left = deadline - monotonic()
        if found is not None or left <= 0:
            return found
        sleep(min(poll, left))


def _route_chair_converse_ask(a: argparse.Namespace) -> int:
    if a.kind not in chair_chat.QUESTION_KINDS:
        print(f"converse: unknown question {a.kind!r}; expected one of {', '.join(chair_chat.QUESTION_KINDS)}")
        return 2
    sent = _converse_append(a, "question", {"kind": a.kind, "subject": a.subject})
    if sent is None:
        return 2
    answer = _await_chat_answer(sent[0] / "courier.jsonl", sent[1], a.wait)
    print(answer["note"] if answer else f"converse: the chair has not answered within {a.wait:g}s; the question stays queued for its next tick")
    return 0


def _route_chair_converse_state(a: argparse.Namespace) -> int:
    workspace = _courier_workspace(a)
    if workspace is None:
        return 2
    state, epics = _read_chat_doc(_chat_state_path(workspace))
    sent = courier.entries(courier.read_text(workspace / "courier.jsonl"))
    recent = [{"event": e["event"]} for e in sent if e.get("topic") == chair_chat.LABEL and e.get("event") in ("directive_applied", "directive_refused")][-5:]
    print(chair_chat.state_summary(state, epics, recent))
    print("Epics are as of the chair's last acting tick; a standby or dry-run tick does not refresh them.")
    return 0


def _is_chair_run_command(command: str) -> bool:
    """Pure. True when the argv has `chair` directly followed by `run`, whatever the executable or the holder label."""
    return " chair run " in f" {' '.join(command.split())} "


def _pid_command(pid: int) -> str:
    """Edge. The command line of `pid` from `ps`; empty when it cannot be read."""
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _is_service_cgroup(text: str) -> bool:
    """Pure. True when the cgroup text of a process names the coxswain chair service unit."""
    return chair_service.UNIT_NAME in text


def _pid_cgroup(pid: int) -> str:
    """Edge. The cgroup text of `pid` from proc; empty when it cannot be read (no proc on macOS, or the pid is gone)."""
    try:
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _aware_or_none(value: object) -> datetime.datetime | None:
    """An aware datetime from a datetime or ISO text; naive reads as UTC, anything unparseable as None."""
    try:
        at = value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=datetime.UTC)


def _holder_from_row(row: Mapping[str, object] | None) -> tuple[object, datetime.datetime | None] | None:
    """The chair lease row as (HolderLease, last tick), or None when nobody holds it.

    Only a chair loop writes a `tick_at` into the row's `status` cell, so a holder with one is the loop and a Claude
    session holder is not. A row with no `status` column cannot say, so it counts as a loop, as `_attach_inputs` reads it.
    An expiry that does not parse reads as already expired."""
    holder = str((row or {}).get("holder") or "")
    label, _, rest = holder.partition("@")
    host, _, pid_text = rest.rpartition(":")
    if row is None or not label or not host or not pid_text.isdigit():
        return None
    doc = run_store.json_cell(row.get("status")) if row.get("status") else None
    tick = _aware_or_none(doc.get("tick_at")) if isinstance(doc, dict) and doc.get("tick_at") else None
    expires = _aware_or_none(row.get("expires_at")) or datetime.datetime.fromtimestamp(0, datetime.UTC)
    return chair_holder.HolderLease(label, int(pid_text), host, expires, "status" not in row or tick is not None), tick


def _read_holder_inputs(runs_dir: Path):
    """Edge. (HolderLease, last tick, pid_alive) for the chair lease row; None with no store, no row, or a store error.

    `pid_alive` is None for a holder on another host: this host cannot probe that pid."""
    try:
        opened = run_store.open_store(runs_dir)
        if opened is None:
            return None
        conn, token = opened
        try:
            row = conn.execute(run_store.sql_for("SELECT * FROM leases WHERE name = {p}", token), (chair.LEASE_NAME,)).fetchone()
        finally:
            conn.close()
    except (*run_store._DB_ERRORS, RuntimeError, OSError, ValueError):
        return None
    held = _holder_from_row(None if row is None else dict(row))
    if held is None:
        return None
    lease, tick = held
    return lease, tick, chair.pid_alive(lease.pid) if lease.host == socket.gethostname() else None


def _loop_holder(label: str, pid: int, host: str) -> chair_resolve.SessionHolder:
    """Edge. A live chair loop holder; the service form when its cgroup names the chair service (readable only on this host)."""
    return chair_resolve.SessionHolder("loop", label, host == socket.gethostname() and _is_service_cgroup(_pid_cgroup(pid)), host)


def _live_loop_holder(runs_dir: Path) -> chair_resolve.SessionHolder | None:
    """The live chair loop that holds the chair, or None when the chair may be taken.

    The lease row names a loop by its tick. A service whose row has no tick yet, or one only the runs chair record names,
    is still a loop when its pid is alive on this host and its command line is a chair run command."""
    this_host = socket.gethostname()
    inputs = _read_holder_inputs(runs_dir)
    if inputs is not None:
        lease, tick, alive = inputs
        if not lease.is_loop and alive and _is_chair_run_command(_pid_command(lease.pid)):
            lease = dataclasses.replace(lease, is_loop=True)
        verdict = chair_holder.chair_holder_verdict(
            lease, this_host, datetime.datetime.now(datetime.UTC), tick, alive, float(chair.DEFAULT_LEASE_TTL_SECONDS)
        )
        if verdict.kind != "none":
            return _loop_holder(lease.label, lease.pid, lease.host)
    try:
        record = chair.read(runs_dir)
    except (OSError, ValueError):
        return None
    pid = None if record is None else record.get("pid")
    if isinstance(pid, int) and record is not None and record.get("host") == this_host and chair.pid_alive(pid) and _is_chair_run_command(_pid_command(pid)):
        return _loop_holder(str(record.get("session") or "?"), pid, this_host)
    return None


def _launcher_session_takes(profile_arg: str | None) -> str:
    """The profile's `chair: session_takes:` policy; when_free when the profile cannot be read or does not say."""
    text = _read_text_or_none(_profile_path(argparse.Namespace(profile=profile_arg)))
    try:
        chair_keys = (route.parse_profile(text) if text is not None else {}).get("chair")
    except route.ProfileError:
        chair_keys = None
    return chair_resolve.resolve_session_takes(chair_keys.get("session_takes") if isinstance(chair_keys, dict) else None)
