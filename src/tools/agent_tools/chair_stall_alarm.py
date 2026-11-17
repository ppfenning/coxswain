"""Pure stall alarm for the landing model (docs/hardening.md H7.1): tell the operator when the loop has stopped moving.

Three checks, each a fact the chair already has:

- **no land**: initiatives are open (a landing record not `landed` and not `draft`) and nothing has landed, no phase
  merged into its initiative branch and no initiative landed on its parent, for `land_hours`. The clock starts at the
  newest land, or at the oldest open record's creation when that is later, so a fresh initiative is not late at once.
- **no launch**: lanes are free and work is ready (`idle_stall`'s counts), this tick plans no launch, and the newest
  done launch is at least `launch_minutes` old. With no launch in the action window the age is unknown and nothing
  is raised, as `chair_idle_stall` reads an unknown age.
- **same failure**: one action (its kind and target) failed or was refused with the same reason on each of its last
  `repeat_ticks` attempts, and the newest of them is recent (`STREAK_LIVE`), so a streak the chair stopped attempting
  falls quiet instead of standing for the whole action window.

A stall raises one `stall_alarm` and, once its check no longer holds, one `stall_clear`. Which stalls are raised is read
back from the chair's own action rows (`open_alarms`), so the alarm needs no state of its own. Settings live in the
profile under `chair.stall_alarm` (`land_hours`, `launch_minutes`, `repeat_ticks`); 0 turns one check off.
No I/O, no clock: `now` is an argument.
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
_NOT_ATTEMPTS = frozenset({*ALARM_KINDS, "needs_chair"})  # rows that report on the loop and are not an attempt of work
_OPEN_EXCLUDED = frozenset({"landed", "draft"})
_NUMBER = re.compile(r"\d+")
_REASON_CHARS = 200


class Settings(TypedDict):
    land_hours: float
    launch_minutes: float
    repeat_ticks: int


class Streak(TypedDict):
    """One action's newest run of failures with the same reason: its kind, target and initiative, the reason, how many
    attempts in a row, and the first and newest attempt's time."""

    kind: str
    target: str
    initiative: str
    reason: str
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
    open_since: str | None
    open_initiatives: list[str]
    last_launch_at: str | None
    streaks: list[Streak]
    open: dict[str, OpenAlarm]


class Stall(TypedDict):
    key: str  # what stalled, stable while it stands: `no_land`, `no_launch`, `repeat:<kind>:<target>:<digest>`
    since: str
    subject: str  # the initiative (or task) the courier entry points at
    detail: str


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


def _newest(stamps: Sequence[object]) -> str | None:
    dated = [(d, str(s)) for s in stamps if (d := parse_ts(s)) is not None]
    return max(dated)[1] if dated else None


def land_facts(records: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None, list[str]]:
    """From the store's landing records: the newest land (a phase `merged` into its initiative branch, or a record
    `landed`), the oldest open record's creation, and the open initiatives, sorted. Open is any state but landed or draft."""
    lands = [
        *(r.get("updated_at") for r in records if r.get("state") == "landed"),
        *(
            p.get("updated_at")
            for r in records
            for p in (r.get("phases") or {}).values()
            if isinstance(p, Mapping) and p.get("state") == "merged"
        ),
    ]
    live = [r for r in records if str(r.get("state") or "building") not in _OPEN_EXCLUDED]
    created = [(d, str(r.get("created_at"))) for r in live if (d := parse_ts(r.get("created_at"))) is not None]
    return _newest(lands), (min(created)[1] if created else None), sorted(str(r.get("initiative") or "") for r in live)


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


def failure_streaks(docs: Sequence[Mapping[str, Any]], repeat_ticks: int) -> list[Streak]:
    """Per action (kind and target), its newest attempts in a row that failed with the same reason, when they number at
    least `repeat_ticks`; none when `repeat_ticks` is 0. Rows are the chair's action rows, newest anywhere in the list;
    alarm and needs_chair rows are reports, not attempts, and are skipped. Sorted by kind and target."""
    if repeat_ticks < 2:
        return []
    attempts: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for doc in _by_time([d for d in docs if d.get("kind") and d.get("kind") not in _NOT_ATTEMPTS]):
        attempts.setdefault((str(doc["kind"]), str(doc.get("target") or "")), []).append(doc)
    found: list[Streak] = []
    for (kind, target), rows in sorted(attempts.items()):
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
                "count": run, "since": str(rows[-run]["ts"]), "newest": str(newest["ts"]),
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
    """Everything the three checks read: the landing records' land facts and the action rows' launches, failure
    streaks and open alarms."""
    last_land, open_since, initiatives = land_facts(records)
    return {
        "settings": settings,
        "last_land_at": last_land,
        "open_since": open_since,
        "open_initiatives": initiatives,
        "last_launch_at": last_launch_at(docs),
        "streaks": failure_streaks(docs, settings["repeat_ticks"]),
        "open": open_alarms(docs),
    }


def _later(a: str | None, b: str | None) -> str | None:
    pa, pb = parse_ts(a), parse_ts(b)
    if pa is None or pb is None:
        return a if pb is None else b
    return a if pa >= pb else b


def _hours(td: timedelta) -> str:
    return f"{td.total_seconds() / 3600:.1f}h"


def no_land(facts: StallFacts, now: datetime) -> Stall | None:
    hours = facts["settings"]["land_hours"]
    initiatives = facts["open_initiatives"]
    since = _later(facts["last_land_at"], facts["open_since"])
    start = parse_ts(since)
    if hours <= 0 or not initiatives or start is None or now - start < timedelta(hours=hours):
        return None
    last = f"the last land was at {facts['last_land_at']}" if facts["last_land_at"] else "nothing has landed yet"
    named = ", ".join(initiatives[:5]) + (f" and {len(initiatives) - 5} more" if len(initiatives) > 5 else "")
    return {"key": "no_land", "since": str(since), "subject": initiatives[0],
            "detail": f"no land for {_hours(now - start)} while {len(initiatives)} initiative(s) are open ({named}); {last}"}


def no_launch(facts: StallFacts, free_lanes: int, ready: int, launching: bool, subject: str, now: datetime) -> Stall | None:
    minutes = facts["settings"]["launch_minutes"]
    last = parse_ts(facts["last_launch_at"])
    if minutes <= 0 or free_lanes <= 0 or ready <= 0 or launching or last is None or now - last < timedelta(minutes=minutes):
        return None
    return {"key": "no_launch", "since": str(facts["last_launch_at"]), "subject": subject,
            "detail": f"no launch for {round((now - last).total_seconds() / 60)}m with {free_lanes} free lane(s) and {ready} "
                      f"ready; the last launch was at {facts['last_launch_at']}"}


def repeat_key(streak: Streak) -> str:
    digest = hashlib.sha1(streak["reason"].encode("utf-8")).hexdigest()[:12]
    return f"repeat:{streak['kind']}:{streak['target']}:{digest}"


def repeats(facts: StallFacts, now: datetime) -> list[Stall]:
    ticks = facts["settings"]["repeat_ticks"]
    return [
        {"key": repeat_key(s), "since": s["since"], "subject": s["initiative"],
         "detail": f"{s['kind']} {s['target']} failed {s['count']} times in a row with: {s['reason'][:_REASON_CHARS]}"}
        for s in facts["streaks"]
        if ticks >= 2 and s["count"] >= ticks and (newest := parse_ts(s["newest"])) is not None and now - newest <= STREAK_LIVE
    ]


def observe(facts: StallFacts, free_lanes: int, ready: int, launching: bool, subject: str, now: datetime) -> list[Stall]:
    """The stalls standing now, in a fixed order: no land, no launch, then each repeated failure."""
    found = [no_land(facts, now), no_launch(facts, free_lanes, ready, launching, subject, now)]
    return [*(s for s in found if s is not None), *repeats(facts, now)]


def plan_alarms(standing: Sequence[Stall], opened: Mapping[str, OpenAlarm]) -> list[dict[str, Any]]:
    """One stall_alarm per standing stall not raised already, then one stall_clear per raised stall no longer standing."""
    keys = {s["key"] for s in standing}
    raised = [
        {"kind": RAISE, "stall": s["key"], "since": s["since"], "subject": s["subject"], "detail": s["detail"]}
        for s in standing if s["key"] not in opened
    ]
    cleared = [
        {"kind": CLEAR, "stall": key, "since": alarm["since"], "subject": alarm["subject"], "detail": f"cleared: {alarm['detail']}"}
        for key, alarm in sorted(opened.items()) if key not in keys
    ]
    return [*raised, *cleared]


def plan_stall_alarm(
    facts: Mapping[str, Any], planned: Collection[Mapping[str, Any]], now: datetime, *, launch_paused: bool = False
) -> list[dict[str, Any]]:
    """The tick's alarm actions from `facts["stall_alarm"]` (none without it), `facts["idle_stall"]`'s free lanes and
    ready count, and the tick's planned actions: a planned launch means the lanes are not idle this tick. Under
    `launch_paused` (the hard stop) nothing launches on purpose, so the no-launch check does not stand."""
    alarm = facts.get("stall_alarm")
    if not alarm:
        return []
    idle = facts.get("idle_stall") or {}
    ready = int(idle.get("ready") or 0) + int(idle.get("queued") or 0)
    launching = launch_paused or any(a.get("kind") in LAUNCH_KINDS for a in planned)
    subject = next((str(i["id"]) for i in facts.get("initiatives") or [] if i.get("ready_tasks")), "")
    return plan_alarms(observe(alarm, int(idle.get("free_lanes") or 0), ready, launching, subject, now), alarm["open"])
