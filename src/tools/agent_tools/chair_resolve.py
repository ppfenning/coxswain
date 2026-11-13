"""Bare `cox` resolves the chair before it starts claude: take it, resume a crashed holder, offer a takeover. Pure plan; the takeover is the edge."""

from __future__ import annotations

import datetime
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_tools import chair, leader_chat

TAKEOVER_GRACE_SECONDS = 20.0
TAKEOVER_POLL_SECONDS = 1.0
_TAKE_POLICIES = ("never", "when_free", "always")
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
    note: str | None = None


@dataclass(frozen=True)
class SessionHolder:
    """`free` and `stale` are both a lease nobody holds; `is_service` marks the service among loops."""

    kind: Literal["free", "stale", "loop", "session"]
    label: str | None = None
    is_service: bool = False
    host: str | None = None


@dataclass(frozen=True)
class TakeDecision:
    """`action` is take or observer; `line` is the sentence to show."""

    action: str
    line: str


def transcript_path(config_dir: Path, cwd: str, session_id: str) -> Path:
    """Pure. Claude Code's project slug replaces every non-alphanumeric of the cwd with `-`."""
    return config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"


def resolve_bare(
    record: dict[str, Any] | None, state: str, host: str, now: datetime.datetime, transcript_exists: Callable[[str, str | None], bool],
) -> Plan:
    """Pure. Resume or attach only a recorded session whose transcript `transcript_exists(session_id, cwd)` finds; a remote holder is never checked or attached."""
    if record is None or state in ("none", "stale") or (state == "live" and chair.expired(record, now)):
        return Plan("take")
    sid = chair.holder_session_id(record)
    label, cwd, holder_host = record.get("session"), record.get("cwd"), record.get("host")
    if state == "crashed":
        if not sid:
            return Plan("take")
        if transcript_exists(sid, cwd):
            return Plan("resume", label, sid, cwd, holder_host)
        return Plan("take", note=f"chair: recorded session {sid} was not found; a fresh chair is starting")
    attach = holder_host == host and bool(sid) and transcript_exists(sid, cwd)  # a remote holder's transcript is on its own machine
    return Plan("offer", label, sid, cwd, holder_host, "attach" if attach else "observer")


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
    attachable = plan.default == "attach"
    keys = "(a) take over  (b) open a non-chair session  (c) cancel" + ("  (d) attach" if attachable else "")
    return f"chair: {keys}  [Enter: {'attach' if attachable else 'open a non-chair session'}]"


def choose(key: str, default: str) -> str:
    """Pure. a takes over, b opens a non-chair session, d attaches only when the default is attach, c or anything else cancels; Enter takes the default."""
    return {
        "a": "takeover", "b": "observer", "c": "cancel", "d": "attach" if default == "attach" else "cancel",
        "\r": default, "\n": default, "": default,
    }.get(key.lower(), "cancel")


def resolve_session_takes(raw: object) -> str:
    """Pure. never, when_free or always, ignoring case and surrounding space; anything else is when_free."""
    value = raw.strip().lower() if isinstance(raw, str) else ""
    return value if value in _TAKE_POLICIES else "when_free"


def held_line(label: str, is_service: bool) -> str:
    return f"chair held by {label} ({'service' if is_service else 'chair loop'})"


def session_take_decision(policy: object, holder: SessionHolder, explicit_take: bool) -> TakeDecision:
    """Pure. A live chair loop is observer unless `explicit_take`; an unrecognised holder kind is observer."""
    mode = resolve_session_takes(policy)
    if holder.kind == "loop":
        line = held_line(holder.label or "?", holder.is_service)
        if explicit_take:
            return TakeDecision("take", f"chair: {line}; taking it because the operator asked")
        return TakeDecision("observer", line)
    if holder.kind in ("free", "stale"):
        if mode == "never":
            return TakeDecision("observer", "chair: free, and the policy never takes it; a non-chair session is starting")
        return TakeDecision("take", "chair: free; taking the chair as the session starts")
    if holder.kind == "session":
        if mode == "always":
            return TakeDecision("take", "chair: held by another session; the policy always takes it over")
        return TakeDecision("observer", "chair: held by another session; the offer flow decides")
    return TakeDecision("observer", "chair: holder not recognised; a non-chair session is starting")


def session_start_decision(
    policy: object, record: Mapping[str, Any] | None, live: SessionHolder | None, caller_holds_record: bool, *, caller_label: str | None = None,
) -> TakeDecision:
    """Pure. The SessionStart hook's decision. The hook source (startup, resume, compact, clear) cannot change the outcome, so it is not an argument.

    The caller is the live holder only when `caller_label`, its own label, equals the live holder's. The record's label cannot stand in: the live holder wrote the record, so a resumed session that matches it by session id would pass as the holder.
    """
    if live is None:
        return session_take_decision(policy, SessionHolder("free"), False)
    is_live_holder = caller_holds_record and record is not None and bool(caller_label) and caller_label == live.label == record.get("session")
    if is_live_holder:
        return TakeDecision("take", "chair: held by this session")
    line = f"chair: held by {live.label or '?'} on {live.host or '?'}"
    if resolve_session_takes(policy) == "always":
        return TakeDecision("take", f"{line}; the policy always takes it over")
    return TakeDecision("observer", f"{line}; not the chair")


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
