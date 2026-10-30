"""The chair lock, `runs/chair.json`: pure liveness/take/beat/release/clear over the (session, pid, host) triple; a malformed record reads as stale."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import socket
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_tools import store_cli, store_dialect

__all__ = [
    "CHAIR_FILENAME",
    "DEFAULT_HEARTBEAT_MINUTES",
    "DEFAULT_LEASE_TTL_SECONDS",
    "LEASE_FILENAME",
    "LEASE_NAME",
    "acquire_lease",
    "adopt_session",
    "beat",
    "beat_loop",
    "chair_path",
    "claude_session_from_env",
    "clear",
    "expired",
    "extend",
    "guard",
    "holder_session_id",
    "honour_release_request",
    "hours_left",
    "is_caller_holder",
    "leader_path",
    "lease_holder",
    "lease_verdict",
    "liveness",
    "locked",
    "pid_alive",
    "read",
    "release",
    "release_lease",
    "release_requested",
    "renew_lease",
    "request_release",
    "resolve_holder_label",
    "session_line",
    "settle_resumed",
    "take",
    "write",
]

CHAIR_FILENAME = "chair.json"
_LEGACY_LEADER_FILENAME = "leader.json"

DEFAULT_HEARTBEAT_MINUTES = 10

LEASE_NAME = "chair"
# Beside chair.json, never inside it: the lock file format is unchanged. Holds the holder and epoch that renew and release need.
LEASE_FILENAME = "chair.lease.json"
DEFAULT_LEASE_TTL_SECONDS = DEFAULT_HEARTBEAT_MINUTES * 60


def _is_holder(record: dict[str, Any], session: str, pid: int, host: str) -> bool:
    """A caller holds `record` only when its session, pid and host all match what     was recorded — the label alone is not proof of identity."""
    return record.get("session") == session and record.get("pid") == pid and record.get("host") == host


def liveness(record: dict[str, Any] | None, pid_alive_: bool, now: datetime.datetime, host: str, heartbeat_minutes: int = DEFAULT_HEARTBEAT_MINUTES) -> str:
    """Pure: pid_alive_ is consulted only when record's host is host; a fresh heartbeat from another host is live regardless."""
    if record is None:
        return "none"
    try:
        heartbeat_at = datetime.datetime.fromisoformat(record["heartbeat_at"])
        age = now - heartbeat_at
    except (KeyError, TypeError, ValueError):
        return "stale"
    if age > datetime.timedelta(minutes=heartbeat_minutes):
        return "stale"
    if record.get("host") == host and not pid_alive_:
        return "crashed"
    return "live"


def _until(record: dict[str, Any] | None) -> datetime.datetime | None:
    """The record's `until` as an aware datetime; a missing, malformed or naive one reads as None."""
    try:
        parsed = datetime.datetime.fromisoformat(record["until"])
    except (KeyError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def expired(record: dict[str, Any] | None, now: datetime.datetime) -> bool:
    """Pure: the record carries an `until` before `now`. No `until` never expires."""
    until = _until(record)
    return until is not None and until < now


def hours_left(record: dict[str, Any] | None, now: datetime.datetime) -> float | None:
    """Pure: hours from `now` to the record's `until`, negative once past; None with no usable `until`."""
    until = _until(record)
    return None if until is None else (until - now).total_seconds() / 3600


def extend(record: dict[str, Any], now: datetime.datetime, hours: float) -> dict[str, Any]:
    """Pure: a copy of `record` whose `until` is `hours` after `now`."""
    return {**record, "until": (now + datetime.timedelta(hours=hours)).isoformat()}


def _held_by_line(record: dict[str, Any]) -> str:
    until = f" until {record['until']}" if "until" in record else ""
    return f"held by {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')}{until}"


def take(
    record: dict[str, Any] | None,
    session: str,
    pid: int,
    host: str,
    now: datetime.datetime,
    heartbeat_minutes: int,
    pid_alive_: bool,
    steal: bool = False,
    claude_session: str | None = None,
    hours: float | None = None,
    model: str | None = None,
    cwd: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Pure. A live record past its `until` is taken like a stale one. `hours` stamps the new record's `until`; a None `model`, `claude_session` or `cwd` leaves its key absent. `session_id` repeats `claude_session`, the key bare `cox` resumes by."""
    state = liveness(record, pid_alive_, now, host, heartbeat_minutes)
    lapsed = state == "live" and expired(record, now)
    if state == "live" and not lapsed:
        return None, f"chair: {_held_by_line(record)}"
    if (lapsed or state in ("stale", "crashed")) and not steal:
        why = f"takeover expired at {record['until']}" if lapsed else state
        return None, f"chair: {_held_by_line(record)} ({why}; pass --steal to take over)"
    taken_at = now.isoformat()
    base = {"session": session, "pid": pid, "host": host, "taken_at": taken_at, "heartbeat_at": taken_at, "runs": [], "claude_session": claude_session}
    extras = {**({"model": model} if model is not None else {}), **({"session_id": claude_session} if claude_session else {}), **({"cwd": cwd} if cwd else {})}
    new_record = {**base, **extras}
    return (new_record if hours is None else extend(new_record, now, hours)), ""


def same_holder(record: dict[str, Any] | None, session: str, pid: int, host: str) -> bool:
    """Pure: the record names this session, pid and host. No record names no one."""
    return record is not None and _is_holder(record, session, pid, host)


def claude_session_from_env(environ: Mapping[str, str]) -> str | None:
    """Edge helper. The chair's Claude session id from CLAUDE_CODE_SESSION_ID, else CLAUDE_SESSION_ID, else None; absence never fails."""
    return environ.get("CLAUDE_CODE_SESSION_ID") or environ.get("CLAUDE_SESSION_ID") or None


def is_caller_holder(record: dict[str, Any] | None, environ: Mapping[str, str], host: str) -> bool:
    """Pure. The caller holds `record` when it is on the record's host and its CLAUDE_PID equals the recorded pid, or its Claude session id equals the recorded one."""
    if record is None or record.get("host") != host:
        return False
    pid_text = environ.get("CLAUDE_PID", "")
    recorded_session = record.get("session_id") or record.get("claude_session")
    by_pid = pid_text.isdigit() and int(pid_text) == record.get("pid")
    return by_pid or (recorded_session is not None and recorded_session == claude_session_from_env(environ))


def resolve_holder_label(record: dict[str, Any] | None, state: str, environ: Mapping[str, str], host: str, explicit: str | None) -> str | None:
    """Pure. An explicit label stands, so a mismatch still reaches `guard`. Without one, a live record the caller holds names its own session."""
    if explicit:
        return explicit
    if record is not None and state == "live" and is_caller_holder(record, environ, host):
        return record.get("session")
    return None


def adopt_session(record: dict[str, Any] | None, state: str, environ: Mapping[str, str], host: str) -> dict[str, Any] | None:
    """Pure. A live lease the caller holds by pid but that recorded no session gets the caller's, so a later bare `cox` can resume it. Returns `record` itself when nothing changes."""
    sid = claude_session_from_env(environ)
    if record is None or state != "live" or sid is None or holder_session_id(record) or not is_caller_holder(record, environ, host):
        return record
    return {**record, "session_id": sid, "claude_session": sid}


def holder_session_id(record: Mapping[str, Any]) -> str | None:
    return record.get("session_id") or record.get("claude_session") or None


def release_requested(record: dict[str, Any] | None) -> bool:
    """Pure. A takeover has asked the record's holder to release."""
    return record is not None and bool(record.get("release_requested_by"))


def request_release(record: dict[str, Any], by: str, now: datetime.datetime) -> dict[str, Any]:
    """Pure. A copy of `record` carrying the takeover's request; `beat` keeps unknown keys, so it stays until the holder honours it."""
    return {**record, "release_requested_by": by, "release_requested_at": now.isoformat()}


def session_line(record: dict[str, Any] | None, state: str, environ: Mapping[str, str], host: str) -> str:
    """Pure. The one SessionStart line: this session holds a live chair (`taken_over_from` on the lease, when present, names who it replaced), or it does not."""
    if record is None:
        return "chair: none"
    if state == "live" and is_caller_holder(record, environ, host):
        previous = record.get("taken_over_from")
        if previous:
            return f"chair: taken over from {previous}"
        return f"chair: resumed {record.get('session', '?')}" if record.get("resumed") else "chair: held by this session"
    return f"chair: held by {record.get('session', '?')} on {record.get('host', '?')}; not the chair"


def settle_resumed(record: dict[str, Any] | None, state: str, environ: Mapping[str, str], host: str) -> dict[str, Any] | None:
    """Pure. The `resumed` stamp belongs to the one SessionStart line that announces it, so the holder's copy drops it. Returns `record` itself when nothing changes."""
    if record is None or not record.get("resumed") or state != "live" or not is_caller_holder(record, environ, host):
        return record
    return {key: value for key, value in record.items() if key != "resumed"}


def beat(
    record: dict[str, Any] | None,
    session: str,
    pid: int,
    host: str,
    now: datetime.datetime,
    run_id: str | None = None,
    claude_session: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Pure. A None `claude_session` keeps the recorded one, so a beat from a shell without the env var does not erase it."""
    if record is None or not _is_holder(record, session, pid, host):
        return None, f"chair: not held by {session} (pid {pid}) on {host}"
    existing_runs = record.get("runs", [])
    runs = existing_runs if run_id is None or run_id in existing_runs else [*existing_runs, run_id]
    kept = claude_session if claude_session is not None else record.get("claude_session")
    return {**record, "heartbeat_at": now.isoformat(), "runs": runs, "claude_session": kept}, ""


def honour_release_request(runs_dir: Path | str, session: str, pid: int, host: str) -> bool:
    """Edge. The one helper every real holder's beat path calls: when a takeover asked this holder to release, ends its lease and lock and returns True."""
    runs_dir = Path(runs_dir)
    with locked(runs_dir):
        record = read(runs_dir)
        if not (release_requested(record) and _is_holder(record, session, pid, host)):
            return False
        release_lease(runs_dir, session, pid, host)
        write(runs_dir, None)
    return True


RELEASE_POLL_SECONDS = 2.0

BEAT_LOOP_DEPRECATION = "chair: beat-loop is deprecated; the chair loop now beats its own lease and this subcommand will be removed in the next release"


def beat_loop(
    label: str,
    pid: int,
    *,
    runs_dir: Path | str,
    interval: float | None = None,
    clock=time,
    alive=os.kill,
    environ: Mapping[str, str] = os.environ,
) -> int:
    """Edge. Beats the chair at `runs_dir` for `label`/`pid` every `interval`
    seconds (default: half of DEFAULT_HEARTBEAT_MINUTES) while `pid` is alive;
    prints `beat`'s reason and returns 2 the first time it can't renew, 0 once
    `alive(pid, 0)` raises `ProcessLookupError`. A `PermissionError` from
    `alive` means the pid is alive but unsignallable, same as `pid_alive`."""
    tick = DEFAULT_HEARTBEAT_MINUTES * 60 / 2 if interval is None else interval
    host = socket.gethostname()
    print(BEAT_LOOP_DEPRECATION, file=sys.stderr)
    while True:
        try:
            alive(pid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            pass
        if honour_release_request(runs_dir, label, pid, host):
            print(f"chair: {label} released at a takeover request")
            return 0
        with locked(runs_dir):
            new_record, reason = beat(read(runs_dir), label, pid, host, datetime.datetime.now(datetime.UTC), claude_session=claude_session_from_env(environ))
            if new_record is None:
                print(f"chair: {reason}")
                return 2
            lost = renew_lease(runs_dir, label, pid, host)
            if lost:
                print(lost)
                return 2
            write(runs_dir, new_record)
        remaining = tick
        while True:
            step = min(remaining, RELEASE_POLL_SECONDS)
            clock.sleep(step)
            remaining -= step
            if remaining <= 0:
                break
            if honour_release_request(runs_dir, label, pid, host):
                print(f"chair: {label} released at a takeover request")
                return 0


def release(record: dict[str, Any] | None, session: str, pid: int, host: str) -> tuple[dict[str, Any] | None, str]:
    """Pure."""
    if record is None:
        return None, "chair: no lock held"
    if not _is_holder(record, session, pid, host):
        return record, f"chair: not held by {session} (pid {pid}) on {host}"
    return None, ""


def guard(record: dict | None, holder: str, state: str) -> str | None:
    """The one-line refusal when a LIVE lock belongs to another holder, else None."""
    if record is None or state != "live" or record.get("session") == holder:
        return None
    return f"refusing: the landing loop is held by {record.get('session')} (live); pass --force to override"


def chair_path(runs_dir: Path) -> Path:
    return Path(runs_dir) / CHAIR_FILENAME


# Back-compat alias for the pre-rename name; consumers still spell this
# `leader_path` until the wire-consumers phase moves them to `chair_path`.
leader_path = chair_path


def read(runs_dir: Path) -> dict[str, Any] | None:
    """Edge. Tries chair.json first; falls back to leader.json so a lock taken before this rename is not orphaned."""
    runs_dir = Path(runs_dir)
    try:
        text = chair_path(runs_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            text = (runs_dir / _LEGACY_LEADER_FILENAME).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
    return json.loads(text)


def write(runs_dir: Path, record: dict[str, Any] | None) -> None:
    """Edge. Always clears the legacy leader.json too, so a lock read through the fallback is actually released, and a beat leaves at most one lock file behind."""
    runs_dir = Path(runs_dir)
    path = chair_path(runs_dir)
    legacy = runs_dir / _LEGACY_LEADER_FILENAME
    if record is None:
        path.unlink(missing_ok=True)
        legacy.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    legacy.unlink(missing_ok=True)


def lease_holder(session: str, pid: int, host: str) -> str:
    """Pure. The one holder string the store lease sees for a (session, pid, host) triple."""
    return f"{session}@{host}:{pid}"


def lease_verdict(result: store_cli.LeaseResult, store_url: str | None = None) -> tuple[str, str]:
    """Pure. ("granted" | "refused" | "fallback", line). An error, a released answer or no harness refuses on a postgres `store_url` and falls back to the lock file otherwise."""
    if isinstance(result, store_cli.LeaseGranted):
        return "granted", ""
    if isinstance(result, store_cli.LeaseRefused):
        return "refused", f"chair: held by {result.holder} (store lease)" if result.holder else "chair: the store lease was refused"
    shared = store_url is not None and store_dialect.is_postgres(store_url)
    if isinstance(result, store_cli.LeaseError):
        detail = result.detail
        warning = f"chair: warning: store lease failed ({detail}); using the lock file only"
    elif isinstance(result, store_cli.LeaseReleased):
        detail = "the lease answered released, not granted"
        warning = "chair: warning: store lease answered released, not granted; using the lock file only"
    else:
        detail = "no harness found"
        warning = "chair: warning: no harness found; using the lock file only"
    if shared:
        return "refused", f"chair: cannot reach the store lease ({detail}); not taking the chair"
    return "fallback", warning


def _lease_path(runs_dir: Path) -> Path:
    return Path(runs_dir) / LEASE_FILENAME


def _read_lease(runs_dir: Path, holder: str) -> dict[str, Any] | None:
    """Edge. The sidecar record when it is this holder's and carries an integer epoch, else None."""
    try:
        parsed = json.loads(_lease_path(runs_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("holder") == holder and isinstance(parsed.get("epoch"), int):
        return parsed
    return None


def _write_lease(runs_dir: Path, holder: str, epoch: int) -> None:
    path = _lease_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"holder": holder, "epoch": epoch}), encoding="utf-8")


def acquire_lease(runs_dir: Path, session: str, pid: int, host: str, ttl: int = DEFAULT_LEASE_TTL_SECONDS, steal: bool = False) -> str:
    """Edge. The refusal line when another holder has a live lease, else "". On a postgres store a lease error, a released answer or a missing harness also returns a refusal line, because the lock file cannot see a chair on another machine. On the files store they warn on stderr and return "", so the lock file alone governs. `steal` takes a held lease at a higher epoch. `cox chair take` shares this function and refuses the same way."""
    holder = lease_holder(session, pid, host)
    result = store_cli.lease_acquire(runs_dir, LEASE_NAME, holder, ttl, steal)
    if isinstance(result, store_cli.LeaseGranted):
        _write_lease(runs_dir, holder, result.epoch)
        return ""
    verdict, line = lease_verdict(result, store_cli._store_url(Path(runs_dir)))
    if verdict == "refused":
        return line
    _lease_path(runs_dir).unlink(missing_ok=True)
    print(line, file=sys.stderr)
    return ""


def renew_lease(runs_dir: Path, session: str, pid: int, host: str, ttl: int = DEFAULT_LEASE_TTL_SECONDS) -> str:
    """Edge. The refusal line when the lease is lost to another holder, else "". With no sidecar (a chair taken before
    the lease existed, or one that fell back to the lock file), the beat tries to acquire it instead. A held lease that
    cannot be renewed warns and keeps the chair even on a postgres store, so one store blip does not abandon a chair
    that is mid-land. Only acquiring refuses."""
    holder = lease_holder(session, pid, host)
    lease = _read_lease(runs_dir, holder)
    if lease is None:
        return acquire_lease(runs_dir, session, pid, host, ttl)
    result = store_cli.lease_renew(runs_dir, LEASE_NAME, holder, lease["epoch"], ttl)
    if isinstance(result, store_cli.LeaseGranted):
        _write_lease(runs_dir, holder, result.epoch)
        return ""
    verdict, line = lease_verdict(result)
    if verdict == "refused":
        return line
    print(line, file=sys.stderr)
    return ""


def release_lease(runs_dir: Path, session: str, pid: int, host: str) -> None:
    """Edge. Releases the lease and removes the sidecar; only a refusal or a fallback warns, and never blocks."""
    holder = lease_holder(session, pid, host)
    lease = _read_lease(runs_dir, holder)
    if lease is None:
        return
    result = store_cli.lease_release(runs_dir, LEASE_NAME, holder, lease["epoch"])
    if not isinstance(result, (store_cli.LeaseGranted, store_cli.LeaseReleased)):
        print(lease_verdict(result)[1], file=sys.stderr)
    _lease_path(runs_dir).unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    # Chair lock only: a reused pid reads alive here, and the heartbeat in `liveness` is what catches it. Run pidfiles use `epic.run_live`.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def clear(runs_dir: Path, force: bool = False) -> tuple[Path, dict[str, Any]] | None:
    """Edge. Removes whichever lock file is present unless its recorded pid is live and force is False; returns the path and holder removed, or None."""
    runs_dir = Path(runs_dir)
    primary = chair_path(runs_dir)
    path = primary if primary.exists() else runs_dir / _LEGACY_LEADER_FILENAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if pid_alive(record.get("pid")) and not force:
        return None
    path.unlink(missing_ok=True)
    return path, record


@contextlib.contextmanager
def locked(runs_dir: Path):
    """Edge."""
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(runs_dir / "chair.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="python -m agent_tools.chair")
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    beat_loop_parser = subparsers.add_parser("beat-loop", help="DEPRECATED: the chair loop beats its own lease; beat the chair for --label/--pid until the pid is gone")
    beat_loop_parser.add_argument("--label", required=True)
    beat_loop_parser.add_argument("--pid", type=int, required=True)
    beat_loop_parser.add_argument("--runs-dir", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(beat_loop(args.label, args.pid, runs_dir=args.runs_dir))
