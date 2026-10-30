"""Bare `cox` resolves the chair before it starts claude: take it, resume a crashed holder, offer a takeover. Pure plan; the takeover is the edge."""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_tools import chair, leader_chat

TAKEOVER_GRACE_SECONDS = 20.0
TAKEOVER_POLL_SECONDS = 1.0
_SESSION_FLAGS = ("-r", "--resume", "-c", "--continue", "--session-id")


@dataclass(frozen=True)
class Plan:
    """`kind` is take, resume (a crashed holder) or offer (a live one). `default` is what Enter, and `--print-argv`, choose for an offer."""

    kind: str
    label: str | None = None
    session_id: str | None = None
    cwd: str | None = None
    host: str | None = None
    default: str = "observer"


def resolve_bare(record: dict[str, Any] | None, state: str, host: str, now: datetime.datetime) -> Plan:
    """Pure. A crashed holder resumes only when its lease recorded a session; a live local one with a session defaults to attaching; a remote one never does."""
    if record is None or state in ("none", "stale") or (state == "live" and chair.expired(record, now)):
        return Plan("take")
    sid = chair.holder_session_id(record)
    label, cwd, holder_host = record.get("session"), record.get("cwd"), record.get("host")
    if state == "crashed":
        return Plan("resume", label, sid, cwd, holder_host) if sid else Plan("take")
    local = holder_host == host
    return Plan("offer", label, sid, cwd, holder_host, "attach" if local and sid else "observer")


def holder_line(record: Mapping[str, Any]) -> str:
    where = f" in {record['cwd']}" if record.get("cwd") else ""
    return f"chair: held by {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')}{where}, since {record.get('taken_at', '?')}"


def describe(action: str, plan: Plan) -> str:
    """Pure. What bare `cox` would do for `action`, for `--print-argv`."""
    where = f" in {plan.cwd}" if plan.cwd else ""
    return {
        "take": "chair: would take the chair as the session starts",
        "resume": f"chair: would resume {plan.label} with claude --resume {plan.session_id}{where} and take the lease",
        "attach": f"chair: would attach to {plan.label}'s conversation with claude --resume {plan.session_id}{where}, without taking the chair",
        "observer": "chair: would open a non-chair session",
    }.get(action, f"chair: would {action}")


def offer_line(plan: Plan) -> str:
    names = {"attach": "attach", "observer": "open a non-chair session"}
    return f"chair: (a) take over  (b) open a non-chair session  (c) cancel  [Enter: {names[plan.default]}]"


def choose(key: str, default: str) -> str:
    """Pure. a takes over, b opens a non-chair session, c or anything else cancels; Enter takes the default."""
    return {"a": "takeover", "b": "observer", "c": "cancel", "\r": default, "\n": default, "": default}.get(key.lower(), "cancel")


def with_session(argv: list[str], flag: str, value: str, extra_args: list[str]) -> list[str]:
    """Pure. Puts `flag value` after the claude binary unless the operator's own extra args already pick a session."""
    if any(arg in _SESSION_FLAGS for arg in extra_args):
        return argv
    return [argv[0], flag, value, *argv[1:]]


def takeover(
    runs_dir: Path, label: str, pid: int, host: str, claude_session: str | None, cwd: str | None, *, heartbeat_minutes: int,
    clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
    grace: float = TAKEOVER_GRACE_SECONDS, poll: float = TAKEOVER_POLL_SECONDS,
) -> tuple[bool, str]:
    """Edge. Stamps a release request on the lease, which the holder's beat loop honours within seconds, and notes it in the chat thread for a human. Waits `grace` for the holder to go; a holder with no beat loop running is ended after it. Then takes the chair and stamps `taken_over_from`."""
    now = datetime.datetime.now(datetime.UTC)
    with chair.locked(runs_dir):
        old = chair.read(runs_dir)
        if old is not None:
            chair.write(runs_dir, chair.request_release(old, label, now))
    old_label = None if old is None else old.get("session")
    if old is not None:
        chat = leader_chat.chat_path(runs_dir)
        entry = {"at": now.isoformat(), "from": label, "text": f"takeover: {label} asks {old_label} to release the chair"}
        chat.parent.mkdir(parents=True, exist_ok=True)
        with chat.open("a", encoding="utf-8") as thread:  # one appended line, so a concurrent chat reply is never overwritten
            thread.write(leader_chat.append_line("", entry))
        deadline = clock() + grace
        while clock() < deadline and chair.read(runs_dir) is not None:
            sleep(poll)
    with chair.locked(runs_dir):
        current = chair.read(runs_dir)
        if current is not None:
            chair.release_lease(runs_dir, current.get("session", ""), current.get("pid", 0), current.get("host", ""))
            chair.write(runs_dir, None)
        refusal = chair.acquire_lease(runs_dir, label, pid, host, steal=True)
        if refusal:
            return False, refusal
        taken, _ = chair.take(None, label, pid, host, datetime.datetime.now(datetime.UTC), heartbeat_minutes, False, claude_session=claude_session, cwd=cwd)
        chair.write(runs_dir, {**taken, **({"taken_over_from": old_label} if old_label else {})})
    return True, f"chair taken over from {old_label}" if old_label else f"chair taken: {label} (pid {pid}) on {host}"
