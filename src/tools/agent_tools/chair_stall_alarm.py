"""Pure stall alarm for the landing model (docs/hardening.md H7.1): tell the operator when the loop has stopped moving.

Three checks, each a fact the chair already has:

- **no land**: initiatives are open (a landing record not `landed` and not `draft`, and the initiative not held for a
  person) and nothing has landed, no phase merged into its initiative branch and no initiative landed on its parent,
  for `land_hours`. The clock starts at the newest land, the oldest open record's creation, or the chair's first
  recorded action, whichever is latest, so neither a fresh initiative nor a fresh chair is late at once.
- **no launch**: lanes are free and work is ready (`idle_stall`'s counts), and the newest launch that completed is at
  least `launch_minutes` old; with none ever, the clock starts at the chair's first recorded action. A planned launch
  does not count, so a launch refused every tick does not hide the stall. It does not stand while the chair's own
  idle-lanes line (`idle_stall`, `idle_ready`) is raised for the same condition.
- **same failure**: one action (its kind and target) failed or was refused with the same reason on each of its last
  `repeat_ticks` attempts, and the newest of them is recent (`STREAK_LIVE`).

A stall raises one `stall_alarm` and, once its check no longer holds, one `stall_clear`, the all-clear. A same-failure
stall is cleared only when its streak breaks: the action succeeds or fails another way. A streak that goes quiet
instead (no attempt within `STREAK_LIVE`, as under a hard stop, or its rows gone) expires: a `stall_clear` marked
`quiet` closes it with no message. Under the hard stop, nothing launches or is meant to land, so only the same-failure
check stands. Which stalls are raised is read back from the chair's own action rows (`open_alarms`), so the alarm needs
no state of its own. Settings live in the profile under `chair.stall_alarm` (`land_hours`, `launch_minutes`,
`repeat_ticks`); 0 turns one check off. No I/O, no clock: `now` is an argument.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

DEFAULT_LAND_HOURS = 6.0
DEFAULT_LAUNCH_MINUTES = 60.0
DEFAULT_REPEAT_TICKS = 3
STREAK_LIVE = timedelta(hours=1)  # a failure streak whose newest attempt is older than this has stopped being attempted
RAISE, CLEAR = "stall_alarm", "stall_clear"
ALARM_KINDS = frozenset({RAISE, CLEAR})
FAILED_STATUSES = frozenset({"failed", "refused", "not_landed", "merge_failed"})
LAUNCH_KINDS = frozenset({"launch_epic", "relaunch", "retry", "rescue", "launch_decompose"})
IDLE_CAUSES = frozenset({"idle_stall", "idle_ready"})  # the chair's own idle-lanes needs_chair lines, which push already
REPEAT = "repeat"
_NOT_ATTEMPTS = frozenset({*ALARM_KINDS, "needs_chair"})  # rows that report on the loop and are not an attempt of work
_OPEN_EXCLUDED = frozenset({"landed", "draft"})
_NUMBER = re.compile(r"\d+")
_REASON_CHARS = 200


class Settings(TypedDict):
    land_hours: float
    launch_minutes: float
    repeat_ticks: int


class Head(TypedDict):
    """One action's newest attempt: whether it failed, its reason as `normal_reason` reads it, and its time."""

    failed: bool
    reason: str
    ts: str


class Streak(TypedDict):
    """One action's newest run of failures with the same reason: its kind, target and initiative, the reason (as
    `normal_reason` reads it, and the newest attempt's own text), how many in a row, and the first and newest times."""

    kind: str
    target: str
    initiative: str
    reason: str
    raw: str
    count: int
    since: str
    newest: str


class OpenAlarm(TypedDict):
    since: str
    detail: str
    subject: str


class StallFacts(TypedDict):
    """What the checks read, gathered once per tick by `stall_facts`."""

    settings: Settings
    last_land_at: str | None
    open_initiatives: dict[str, str]  # open initiative -> its landing record's creation
    last_launch_at: str | None
    watch_since: str | None  # the chair's oldest recorded action: the clocks start no earlier than this
    heads: dict[str, Head]  # `repeat:<kind>:<target>` -> that action's newest attempt
    streaks: list[Streak]
    open: dict[str, OpenAlarm]


class Stall(TypedDict):
    key: str  # what stalled, stable while it stands: `no_land`, `no_launch`, `repeat:<kind>:<target>:<digest>`
    since: str
    subject: str  # the initiative (or task) the courier entry points at
    detail: str


class Gate(TypedDict):
    """This tick's plan, as the checks need it."""

    free_lanes: int
    ready: int
    subject: str  # the first initiative with ready work, for the no-launch entry
    held: frozenset[str]  # initiatives held for a person: their landing waits on that person, not on the loop
    idle_raised: bool  # the chair's own idle-lanes line stands this tick
    paused: bool  # the hard stop


def _number(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return default
    return float(value)


def resolve_settings(value: object) -> Settings:
    """The profile's raw `chair.stall_alarm` mapping, each key a number of at least 0 (0 turns its check off), else its
    default. `repeat_ticks` is a whole number of at least 2, since one failure is not a repeat; 1 reads as the default."""
    raw = value if isinstance(value, Mapping) else {}
    ticks = raw.get("repeat_ticks")
    whole = not isinstance(ticks, bool) and isinstance(ticks, int | float) and ticks == int(ticks) and (ticks == 0 or ticks >= 2)
    return {
        "land_hours": _number(raw.get("land_hours"), DEFAULT_LAND_HOURS),
        "launch_minutes": _number(raw.get("launch_minutes"), DEFAULT_LAUNCH_MINUTES),
        "repeat_ticks": int(ticks) if whole else DEFAULT_REPEAT_TICKS,  # type: ignore[arg-type]
    }


def parse_ts(text: object) -> datetime | None:
    """An ISO timestamp as an aware UTC datetime; a naive one is read as UTC; None when absent or unreadable."""
    try:
        parsed = datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _dated(stamps: Sequence[object]) -> list[tuple[datetime, str]]:
    return [(d, str(s)) for s in stamps if (d := parse_ts(s)) is not None]


def _newest(stamps: Sequence[object]) -> str | None:
    dated = _dated(stamps)
    return max(dated)[1] if dated else None


def _oldest(stamps: Sequence[object]) -> str | None:
    dated = _dated(stamps)
    return min(dated)[1] if dated else None


def land_facts(records: Sequence[Mapping[str, Any]]) -> tuple[str | None, dict[str, str]]:
    """From the store's landing records: the newest land (a phase `merged` into its initiative branch, or a record
    `landed`), and each open initiative with its record's creation. Open is any state but landed or draft."""
    lands = [
        *(r.get("updated_at") for r in records if r.get("state") == "landed"),
        *(
            p.get("updated_at")
            for r in records
            for p in (r.get("phases") or {}).values()
            if isinstance(p, Mapping) and p.get("state") == "merged"
        ),
    ]
    live = {
        str(r.get("initiative") or ""): str(r.get("created_at") or "")
        for r in records
        if str(r.get("state") or "building") not in _OPEN_EXCLUDED
    }
    return _newest(lands), dict(sorted(live.items()))


def _by_time(docs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows in time order; a row with no readable time is left out. Equal times keep their given order."""
    dated = [(d, n, doc) for n, doc in enumerate(docs) if (d := parse_ts(doc.get("ts"))) is not None]
    return [doc for _, _, doc in sorted(dated, key=lambda t: (t[0], t[1]))]


def last_launch_at(docs: Sequence[Mapping[str, Any]]) -> str | None:
    """The time of the newest done launch row; None with none in the rows."""
    return _newest([d.get("ts") for d in docs if d.get("kind") in LAUNCH_KINDS and d.get("status") == "done"])


def normal_reason(reason: object) -> str:
    """A failure reason with its whitespace collapsed and every number read as `#`, so a count or a pid that moves
    between attempts does not make one failure look like another."""
    return _NUMBER.sub("#", " ".join(str(reason or "").split()))


def action_key(kind: str, target: str) -> str:
    """The prefix every same-failure stall of one action shares: `repeat:<kind>:<target>`."""
    return f"{REPEAT}:{kind}:{target}"


def _attempts(docs: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    """Each action's attempts in time order; alarm and needs_chair rows are reports, not attempts."""
    attempts: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for doc in _by_time([d for d in docs if d.get("kind") and d.get("kind") not in _NOT_ATTEMPTS]):
        attempts.setdefault((str(doc["kind"]), str(doc.get("target") or "")), []).append(doc)
    return attempts


def attempt_heads(docs: Sequence[Mapping[str, Any]]) -> dict[str, Head]:
    """Each action's newest attempt, by `action_key`: what tells a broken streak from one that only went quiet."""
    return {
        action_key(kind, target): {
            "failed": rows[-1].get("status") in FAILED_STATUSES,
            "reason": normal_reason(rows[-1].get("reason")),
            "ts": str(rows[-1]["ts"]),
        }
        for (kind, target), rows in sorted(_attempts(docs).items())
    }


def failure_streaks(docs: Sequence[Mapping[str, Any]], repeat_ticks: int) -> list[Streak]:
    """Per action (kind and target), its newest attempts in a row that failed with the same reason, when they number at
    least `repeat_ticks`; none when `repeat_ticks` is 0. Sorted by kind and target."""
    if repeat_ticks < 2:
        return []
    found: list[Streak] = []
    for (kind, target), rows in sorted(_attempts(docs).items()):
        newest = rows[-1]
        if newest.get("status") not in FAILED_STATUSES:
            continue
        reason = normal_reason(newest.get("reason"))
        run = 0
        for row in reversed(rows):
            if row.get("status") not in FAILED_STATUSES or normal_reason(row.get("reason")) != reason:
                break
            run += 1
        if run >= repeat_ticks:
            found.append({
                "kind": kind, "target": target, "initiative": str(newest.get("initiative") or target), "reason": reason,
                "raw": " ".join(str(newest.get("reason") or "").split()), "count": run, "since": str(rows[-run]["ts"]),
                "newest": str(newest["ts"]),
            })
    return found


def open_alarms(docs: Sequence[Mapping[str, Any]]) -> dict[str, OpenAlarm]:
    """The stalls raised and not yet cleared, by key, from the done alarm rows in time order: the last row per key wins."""
    opened: dict[str, OpenAlarm] = {}
    for doc in _by_time([d for d in docs if d.get("kind") in ALARM_KINDS and d.get("status") == "done" and d.get("stall")]):
        key = str(doc["stall"])
        if doc["kind"] == RAISE:
            opened[key] = {"since": str(doc.get("since") or ""), "detail": str(doc.get("detail") or ""),
                           "subject": str(doc.get("subject") or "")}
        else:
            opened.pop(key, None)
    return opened


def stall_facts(
    settings: Settings, records: Sequence[Mapping[str, Any]], docs: Sequence[Mapping[str, Any]]
) -> StallFacts:
    """Everything the three checks read: the landing records' land facts, and the action rows' launches, first row,
    attempt heads, failure streaks and open alarms."""
    last_land, initiatives = land_facts(records)
    return {
        "settings": settings,
        "last_land_at": last_land,
        "open_initiatives": initiatives,
        "last_launch_at": last_launch_at(docs),
        "watch_since": _oldest([d.get("ts") for d in docs]),
        "heads": attempt_heads(docs),
        "streaks": failure_streaks(docs, settings["repeat_ticks"]),
        "open": open_alarms(docs),
    }


def _hours(td: timedelta) -> str:
    return f"{td.total_seconds() / 3600:.1f}h"


def no_land(facts: StallFacts, held: Collection[str], now: datetime) -> Stall | None:
    """The no-land stall over the open initiatives not held for a person; None while one landed within the hours."""
    hours = facts["settings"]["land_hours"]
    initiatives = {i: created for i, created in facts["open_initiatives"].items() if i not in held}
    if hours <= 0 or not initiatives or facts["watch_since"] is None:
        return None
    since = _newest([facts["last_land_at"], _oldest(list(initiatives.values())), facts["watch_since"]])
    start = parse_ts(since)
    if start is None or now - start < timedelta(hours=hours):
        return None
    last = f"the last land was at {facts['last_land_at']}" if facts["last_land_at"] else "nothing has landed yet"
    names = sorted(initiatives)
    named = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    return {"key": "no_land", "since": str(since), "subject": names[0],
            "detail": f"no land for {_hours(now - start)} while {len(names)} initiative(s) are open ({named}); {last}"}


def no_launch(facts: StallFacts, free_lanes: int, ready: int, subject: str, now: datetime) -> Stall | None:
    """The no-launch stall: free lanes, ready work, and no launch completed within the minutes (or since the chair's
    first recorded action, when none ever did)."""
    minutes = facts["settings"]["launch_minutes"]
    since = facts["last_launch_at"] or facts["watch_since"]
    start = parse_ts(since)
    if minutes <= 0 or free_lanes <= 0 or ready <= 0 or start is None or now - start < timedelta(minutes=minutes):
        return None
    last = f"the last launch was at {facts['last_launch_at']}" if facts["last_launch_at"] else "nothing has launched yet"
    return {"key": "no_launch", "since": str(since), "subject": subject,
            "detail": f"no launch for {round((now - start).total_seconds() / 60)}m with {free_lanes} free lane(s) and "
                      f"{ready} ready; {last}"}


def repeat_key(streak: Streak) -> str:
    digest = hashlib.sha1(streak["reason"].encode("utf-8")).hexdigest()[:12]
    return f"{action_key(streak['kind'], streak['target'])}:{digest}"


def _live(streak: Streak, now: datetime) -> bool:
    newest = parse_ts(streak["newest"])
    return newest is not None and now - newest <= STREAK_LIVE


def repeats(facts: StallFacts, now: datetime) -> list[Stall]:
    """The live same-failure streaks of the set length, each naming its newest attempt's own reason."""
    ticks = facts["settings"]["repeat_ticks"]
    return [
        {"key": repeat_key(s), "since": s["since"], "subject": s["initiative"],
         "detail": f"{s['kind']} {s['target']} failed {s['count']} times in a row; the newest: {s['raw'][:_REASON_CHARS]}"}
        for s in facts["streaks"]
        if ticks >= 2 and s["count"] >= ticks and _live(s, now)
    ]


def observe(facts: StallFacts, gate: Gate, now: datetime) -> list[Stall]:
    """The stalls standing now, in a fixed order: no land, no launch, then each repeated failure. Under the hard stop
    only the repeated failures stand; while the chair's idle-lanes line is raised, no launch does not."""
    found = [] if gate["paused"] else [
        no_land(facts, gate["held"], now),
        None if gate["idle_raised"] else no_launch(facts, gate["free_lanes"], gate["ready"], gate["subject"], now),
    ]
    return [*(s for s in found if s is not None), *repeats(facts, now)]


def repeat_ending(key: str, heads: Mapping[str, Head], now: datetime) -> str:
    """How a raised same-failure stall stands: `standing` while its action's newest attempt is the same failure and
    recent; `broken` once that attempt succeeded or failed another way; `quiet` once there is no recent attempt."""
    prefix, _, digest = key.rpartition(":")
    head = heads.get(prefix)
    when = parse_ts(head["ts"]) if head is not None else None
    if head is None or when is None or now - when > STREAK_LIVE:
        return "quiet"
    same = head["failed"] and hashlib.sha1(head["reason"].encode("utf-8")).hexdigest()[:12] == digest
    return "standing" if same else "broken"


def plan_alarms(standing: Sequence[Stall], facts: StallFacts, now: datetime, paused: bool = False) -> list[dict[str, Any]]:
    """One stall_alarm per standing stall not raised already, then for each raised stall no longer standing one
    stall_clear: the all-clear, or, for a same-failure stall that only went quiet, a `quiet` one that closes it unsaid.
    Under the hard stop (`paused`) a raised no-land or no-launch stall is not judged, so it is neither cleared nor raised."""
    keys = {s["key"] for s in standing}
    opened = facts["open"]
    raised = [
        {"kind": RAISE, "stall": s["key"], "since": s["since"], "subject": s["subject"], "detail": s["detail"]}
        for s in standing if s["key"] not in opened
    ]
    cleared: list[dict[str, Any]] = []
    for key, alarm in sorted(opened.items()):
        if key in keys:
            continue
        repeat = key.startswith(f"{REPEAT}:")
        if paused and not repeat:
            continue
        ending = repeat_ending(key, facts["heads"], now) if repeat else "broken"
        if ending == "standing":
            continue
        quiet = ending == "quiet"
        cleared.append({
            "kind": CLEAR, "stall": key, "since": alarm["since"], "subject": alarm["subject"],
            "detail": f"{'expired' if quiet else 'cleared'}: {alarm['detail']}", **({"quiet": True} if quiet else {}),
        })
    return [*raised, *cleared]


def plan_stall_alarm(
    facts: Mapping[str, Any], planned: Collection[Mapping[str, Any]], now: datetime, *, launch_paused: bool = False
) -> list[dict[str, Any]]:
    """The tick's alarm actions from `facts["stall_alarm"]` (none without it), `facts["idle_stall"]`'s free lanes,
    ready count and open idle line, the held initiatives, and the tick's planned actions, which say whether the chair's
    own idle line goes out this tick. `launch_paused` is the hard stop."""
    alarm = facts.get("stall_alarm")
    if not alarm:
        return []
    idle = facts.get("idle_stall") or {}
    gate: Gate = {
        "free_lanes": int(idle.get("free_lanes") or 0),
        "ready": int(idle.get("ready") or 0) + int(idle.get("queued") or 0),
        "subject": next((str(i["id"]) for i in facts.get("initiatives") or [] if i.get("ready_tasks")), ""),
        "held": frozenset(facts.get("held_initiatives") or ()) | frozenset(facts.get("no_progress") or {}),
        "idle_raised": bool(idle.get("open_signature"))
        or any(a.get("kind") == "needs_chair" and a.get("cause") in IDLE_CAUSES for a in planned),
        "paused": launch_paused,
    }
    return plan_alarms(observe(alarm, gate, now), alarm, now, launch_paused)
