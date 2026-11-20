"""Pure stall alarm for the landing model (docs/hardening.md H7.1): tell the operator when the loop has stopped moving.

Four checks, each a fact the chair already has:

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
- **stranded**: a stranded approved task with no live run has stood for at least `stranded_minutes`, counted from the
  time its row was first seen (the task's last change, else the chair's first recorded action). It clears when the row
  is gone. Nothing launches and nothing fails for such a task, so the other three stay quiet.
- **idle**: the chair has had no live lane and no land in progress for `idle_minutes`, counted from its last progress
  (the newest launch or land that went out, or model call: `idle_stall.last_progress_at`) or its first recorded
  action, whichever is later. A chair restarted long after its last progress has no fresh start to count from, so it
  raises on its first tick. With work held for a person it raises `idle_held:<since>`, naming each held item, its hold
  reason and the command that releases it. With nothing held and no worker capacity (`worker_capacity` is 0) it raises
  `no_capacity:<since>` instead: idle on purpose is a notice, not a stall. Ready work with free lanes is the no-launch
  check's. One idle alarm per spell: a spell ends only on progress newer than its start. A live lane or a land in
  progress pauses it (nothing is raised while busy) but does not end it, so a lane that dies before its first model
  call does not make a second spell. Once progress ends it, its alarm closes quietly, and the next spell starts at that
  progress, so its key is new.

A stall raises one `stall_alarm` and, once its check no longer holds, one `stall_clear`, the all-clear. A same-failure
stall is cleared only when its streak breaks: the action succeeds or fails another way. A streak that goes quiet
instead (no attempt within `STREAK_LIVE`, as under a hard stop, or its rows gone) expires: a `stall_clear` marked
`quiet` closes it with no message. Under the hard stop, nothing launches or is meant to land, so only the same-failure
check stands. Which stalls are raised is read back from the chair's own action rows (`open_alarms`), so the alarm needs
no state of its own. Settings live in the profile under `chair.stall_alarm` (`land_hours`, `launch_minutes`,
`repeat_ticks`, `stranded_minutes`, `idle_minutes`), and the flat `chair.idle_alarm_minutes`, which the profile reader
takes, sets `idle_minutes` (`profile_settings`); 0 turns one check off. No I/O, no clock: `now` is an argument.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NotRequired, TypedDict

from agent_tools.queue_rows import launchable_by_tick
from core.jsontypes import is_list, is_mapping

if TYPE_CHECKING:  # chair_types imports this module for StallFacts, so the import runs only for the checker
    from agent_tools.chair_types import Action, Facts

DEFAULT_LAND_HOURS = 6.0
DEFAULT_LAUNCH_MINUTES = 60.0
DEFAULT_REPEAT_TICKS = 3
DEFAULT_STRANDED_MINUTES = 30.0
DEFAULT_IDLE_MINUTES = 60.0
IDLE_HELD, NO_CAPACITY = "idle_held", "no_capacity"  # the idle alarm's two keys, each raised as `<name>:<since>`
IDLE_KEYS = frozenset({IDLE_HELD, NO_CAPACITY})
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
_HELD_TASK_STATES = frozenset({"blocked", "quarantined"})  # a task held for a person, its move to blocked done or not


class Settings(TypedDict):
    land_hours: float
    launch_minutes: float
    repeat_ticks: int
    stranded_minutes: float
    idle_minutes: float


class HeldItem(TypedDict):
    """One piece of work held for a person: what it is, its initiative, why it is held, and the command that releases it."""

    item: str
    initiative: str
    reason: str
    release: str


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
    told: dict[str, int]  # `<kind>|<stall>` -> its done alarm rows (`told_counts`): a courier id's sequence
    stranded: dict[str, str]  # stranded task with no live run -> when its row was first seen ("" when unknown)
    holds: list[HeldItem]  # the work held for a person in the store (`work_holds`)
    local_host: str  # the chair's own host name, whose hosts-table row says whether its own lanes count


class Stall(TypedDict):
    # what stalled, stable while it stands: `no_land`, `no_launch`, `stranded:<task>`, `repeat:<kind>:<target>:<digest>`,
    # `idle_held:<since>`, `no_capacity:<since>`
    key: str
    since: str
    subject: str  # the initiative (or task) the courier entry points at
    detail: str
    held: NotRequired[list[HeldItem]]  # an idle_held alarm: each held item, carried into its courier entry's fields


class Gate(TypedDict):
    """This tick's plan, as the checks need it."""

    free_lanes: int
    ready: int
    subject: str  # the first initiative with ready work, for the no-launch entry
    held: frozenset[str]  # initiatives held for a person: their landing waits on that person, not on the loop
    idle_raised: bool  # the chair's own idle-lanes line stands this tick
    paused: bool  # the hard stop
    busy: bool  # a lane is live on some machine or a land is in progress; lanes the facts do not show read as busy
    capacity: int  # the lanes the chair could fill for epics (`worker_capacity`)
    held_items: list[HeldItem]  # every task and initiative held for a person (`held_items`)
    progress_at: str | None  # the newest launch, land or model call (`idle_stall.last_progress_at`)


def _number(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return default
    return float(value)


def resolve_settings(value: object) -> Settings:
    """The profile's raw `chair.stall_alarm` mapping, each key a number of at least 0 (0 turns its check off), else its
    default. `repeat_ticks` is a whole number of at least 2, since one failure is not a repeat; 1 reads as the default."""
    raw: Mapping[str, object] = value if is_mapping(value) else {}
    ticks = raw.get("repeat_ticks")
    repeat_ticks = DEFAULT_REPEAT_TICKS
    if not isinstance(ticks, bool) and isinstance(ticks, int | float) and ticks == int(ticks) and (ticks == 0 or ticks >= 2):
        repeat_ticks = int(ticks)
    return {
        "land_hours": _number(raw.get("land_hours"), DEFAULT_LAND_HOURS),
        "launch_minutes": _number(raw.get("launch_minutes"), DEFAULT_LAUNCH_MINUTES),
        "repeat_ticks": repeat_ticks,
        "stranded_minutes": _number(raw.get("stranded_minutes"), DEFAULT_STRANDED_MINUTES),
        "idle_minutes": _number(raw.get("idle_minutes"), DEFAULT_IDLE_MINUTES),
    }


def profile_settings(chair: object) -> dict[str, object]:
    """The raw settings `resolve_settings` reads, from the profile's `chair` mapping: its `stall_alarm` block, with the
    flat `idle_alarm_minutes` (a key the profile reader's flat `chair:` block can carry) as `idle_minutes`."""
    raw: Mapping[str, object] = chair if is_mapping(chair) else {}
    block = raw.get("stall_alarm")
    found: dict[str, object] = dict(block) if is_mapping(block) else {}
    if "idle_alarm_minutes" in raw:
        found["idle_minutes"] = raw["idle_alarm_minutes"]
    return found


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


def _phase_records(record: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The mapping values of a landing record's `phases`; an absent or empty `phases` has none."""
    phases = record.get("phases")
    return [p for p in phases.values() if is_mapping(p)] if is_mapping(phases) else []


def land_facts(records: Sequence[Mapping[str, object]]) -> tuple[str | None, dict[str, str]]:
    """From the store's landing records: the newest land (a phase `merged` into its initiative branch, or a record
    `landed`), and each open initiative with its record's creation. Open is any state but landed or draft."""
    lands = [
        *(r.get("updated_at") for r in records if r.get("state") == "landed"),
        *(
            p.get("updated_at")
            for r in records
            for p in _phase_records(r)
            if p.get("state") == "merged"
        ),
    ]
    live = {
        str(r.get("initiative") or ""): str(r.get("created_at") or "")
        for r in records
        if str(r.get("state") or "building") not in _OPEN_EXCLUDED
    }
    return _newest(lands), dict(sorted(live.items()))


def _by_time(docs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """The rows in time order; a row with no readable time is left out. Equal times keep their given order."""
    dated = [(d, n, doc) for n, doc in enumerate(docs) if (d := parse_ts(doc.get("ts"))) is not None]
    return [doc for _, _, doc in sorted(dated, key=lambda t: (t[0], t[1]))]


def last_launch_at(docs: Sequence[Mapping[str, object]]) -> str | None:
    """The time of the newest done launch row; None with none in the rows."""
    return _newest([d.get("ts") for d in docs if d.get("kind") in LAUNCH_KINDS and d.get("status") == "done"])


def normal_reason(reason: object) -> str:
    """A failure reason with its whitespace collapsed and every number read as `#`, so a count or a pid that moves
    between attempts does not make one failure look like another."""
    return _NUMBER.sub("#", " ".join(str(reason or "").split()))


def action_key(kind: str, target: str) -> str:
    """The prefix every same-failure stall of one action shares: `repeat:<kind>:<target>`."""
    return f"{REPEAT}:{kind}:{target}"


def _attempts(docs: Sequence[Mapping[str, object]]) -> dict[tuple[str, str], list[Mapping[str, object]]]:
    """Each action's attempts in time order; alarm and needs_chair rows are reports, not attempts."""
    attempts: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for doc in _by_time([d for d in docs if d.get("kind") and d.get("kind") not in _NOT_ATTEMPTS]):
        attempts.setdefault((str(doc["kind"]), str(doc.get("target") or "")), []).append(doc)
    return attempts


def attempt_heads(docs: Sequence[Mapping[str, object]]) -> dict[str, Head]:
    """Each action's newest attempt, by `action_key`: what tells a broken streak from one that only went quiet."""
    return {
        action_key(kind, target): {
            "failed": rows[-1].get("status") in FAILED_STATUSES,
            "reason": normal_reason(rows[-1].get("reason")),
            "ts": str(rows[-1]["ts"]),
        }
        for (kind, target), rows in sorted(_attempts(docs).items())
    }


def failure_streaks(docs: Sequence[Mapping[str, object]], repeat_ticks: int) -> list[Streak]:
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


def open_alarms(docs: Sequence[Mapping[str, object]]) -> dict[str, OpenAlarm]:
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


def told_counts(docs: Sequence[Mapping[str, object]]) -> dict[str, int]:
    """How many times each stall was told, by `<kind>|<stall>`: its done raise and clear rows. A failed attempt is not
    counted, so its retry gets the same courier id; the next raise of a key that cleared counts one more, a new id."""
    counts: dict[str, int] = {}
    for d in docs:
        if d.get("kind") in ALARM_KINDS and d.get("status") == "done" and d.get("stall"):
            told = f"{d['kind']}|{d['stall']}"
            counts[told] = counts.get(told, 0) + 1
    return counts


def _extra(row: Mapping[str, object]) -> Mapping[str, object]:
    extra = row.get("extra")
    return extra if is_mapping(extra) else {}


def _one_line(text: object, default: str) -> str:
    return " ".join(str(text or "").split()) or default


def task_release(initiative: str, task: str) -> str:
    """The command that sets a held task back to ready, as `chair_remedy.set_state_argv` spells it, on one line."""
    return " ".join(["cox", "route", "edit", task, *(["--initiative", initiative] if initiative else []), "--state", "ready"])


def work_holds(rows: Sequence[Mapping[str, object]]) -> list[HeldItem]:
    """The work held for a person, from the store's queue rows. First each task whose hold reason carries the
    `needs you:` mark and whose state is `blocked` or `quarantined`: a triage hold writes the reason before it moves
    the task, so a task whose move did not land is held all the same. Then each initiative held on its own row, and
    each non-removed held intake that names an initiative not held on its own row (`release_gate.held_initiatives`),
    released with `cox route release`. Tasks in row order, initiatives in initiative order."""
    from agent_tools.intake_queue import is_held  # lazy: only the idle alarm reads holds off the queue rows
    from core.workstore import HOLD_MARK, HOLD_REASON  # lazy: core sits beside agent_tools in one venv

    tasks: list[HeldItem] = []
    for r in rows:
        reason = _one_line(_extra(r).get(HOLD_REASON), "")
        if r.get("kind") == "task" and r.get("state") in _HELD_TASK_STATES and reason.startswith(HOLD_MARK):
            initiative, task = str(r.get("initiative") or ""), str(r.get("task_id") or "")
            tasks.append({"item": f"{task} in {initiative}", "initiative": initiative, "reason": reason,
                          "release": task_release(initiative, task)})
    own = {
        str(r.get("initiative") or r.get("task_id") or ""): _one_line(_extra(r).get("hold_reason"), "held, no reason given")
        for r in rows if r.get("kind") == "initiative" and is_held(_extra(r))
    }
    initiatives: list[HeldItem] = [
        {"item": i, "initiative": i, "reason": reason, "release": f"cox route release {i}"} for i, reason in own.items() if i
    ]
    for r in rows:
        extra = _extra(r)
        named = str(extra.get("initiative") or "").strip()
        if r.get("kind") != "intake" or r.get("state") == "removed" or not named or named in own or not is_held(extra):
            continue
        intake = str(r.get("task_id") or "")
        initiatives.append({"item": f"intake {intake} for {named}", "initiative": named,
                            "reason": _one_line(extra.get("hold_reason"), "held, no reason given"),
                            "release": f"cox route release {intake}"})
    return [*tasks, *sorted(initiatives, key=lambda h: (h["initiative"], h["item"]))]


def held_items(holds: Sequence[HeldItem], no_progress: Mapping[str, object]) -> list[HeldItem]:
    """Every piece of work held for a person: the store's holds (`work_holds`), then each initiative `no_progress`
    holds this tick that is not held on its row already, with its own hold reason."""
    named = {h["item"] for h in holds}
    pending: list[HeldItem] = [
        {"item": i, "initiative": i, "reason": _one_line(entry.get("reason") if is_mapping(entry) else None, "no progress"),
         "release": f"cox route release {i}"}
        for i, entry in sorted(no_progress.items()) if i not in named
    ]
    return [*holds, *pending]


def stall_facts(
    settings: Settings,
    records: Sequence[Mapping[str, object]],
    docs: Sequence[Mapping[str, object]],
    stranded: Mapping[str, str] | None = None,
    holds: Sequence[HeldItem] = (),
    local_host: str = "",
) -> StallFacts:
    """Everything the checks read: the landing records' land facts, the action rows' launches, first row, attempt
    heads, failure streaks, open alarms and told counts, `stranded`, each stranded task's first-seen time, `holds`,
    the work held for a person (`work_holds`), and `local_host`, the chair's own host name."""
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
        "told": told_counts(docs),
        "stranded": dict(stranded or {}),
        "holds": list(holds),
        "local_host": local_host,
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


def stranded(facts: StallFacts, now: datetime) -> list[Stall]:
    """One stall per stranded task whose row was first seen at least `stranded_minutes` ago, in task order. A task whose
    first-seen time is unknown counts from the chair's first recorded action."""
    minutes = facts["settings"]["stranded_minutes"]
    if minutes <= 0:
        return []
    found: list[Stall] = []
    for task, seen in sorted(facts["stranded"].items()):
        since = seen if parse_ts(seen) is not None else facts["watch_since"]
        start = parse_ts(since)
        if start is None or now - start < timedelta(minutes=minutes):
            continue
        found.append({"key": f"stranded:{task}", "since": str(since), "subject": task,
                      "detail": f"approved task {task} has been stranded with no live run for "
                                f"{round((now - start).total_seconds() / 60)}m"})
    return found


def told_spell(opened: Mapping[str, OpenAlarm], progress_at: str | None) -> Stall | None:
    """The idle alarm already raised for the spell still standing: one whose start no progress has passed since. Only
    progress ends a spell here, never a moved first row, so a spell is told once however long it lasts."""
    progress = parse_ts(progress_at)
    for key, alarm in sorted(opened.items()):
        start = parse_ts(alarm["since"])
        if key.partition(":")[0] in IDLE_KEYS and start is not None and (progress is None or progress <= start):
            return {"key": key, "since": alarm["since"], "subject": alarm["subject"], "detail": alarm["detail"]}
    return None


def held_text(held: Sequence[HeldItem]) -> str:
    """One line per held item: what it is, its hold reason, and the command that releases it."""
    return "\n".join(f"- {h['item']}: {h['reason'][:_REASON_CHARS]} Release: `{h['release']}`" for h in held)


def idle_spell(facts: StallFacts, gate: Gate, now: datetime) -> list[Stall]:
    """The idle alarm standing now: the one already raised for this spell; else, once the chair has been idle for
    `idle_minutes`, `idle_held` when work is held for a person, or `no_capacity` when nothing is held and there are no
    lanes to fill. A told spell stands while busy or under the hard stop, which pause it; only progress ends it. Nothing
    new is raised while busy, under the hard stop, with the check off, or with no time to count from."""
    minutes = facts["settings"]["idle_minutes"]
    if minutes <= 0:
        return []
    told = told_spell(facts["open"], gate["progress_at"])
    if told is not None:
        return [told]
    if gate["paused"] or gate["busy"]:
        return []
    since = _newest([gate["progress_at"], facts["watch_since"]])
    start = parse_ts(since)
    if since is None or start is None or now - start < timedelta(minutes=minutes):
        return []
    held = gate["held_items"]
    spell = f"no launch, land or model call for {_hours(now - start)}, since {since}, and no lane is live"
    if held:
        lanes = ", and no worker capacity either" if gate["capacity"] <= 0 else ""
        return [{"key": f"{IDLE_HELD}:{since}", "since": since, "subject": held[0]["initiative"] or held[0]["item"],
                 "held": list(held),
                 "detail": f"the chair is idle with work held: {spell}{lanes}. {len(held)} item(s) wait on you:\n"
                           f"{held_text(held)}"}]
    if gate["capacity"] <= 0:
        return [{"key": f"{NO_CAPACITY}:{since}", "since": since, "subject": "",
                 "detail": f"every lane host is drained or none is configured, and nothing is held: {spell}. "
                           "The chair is idle on purpose; this is not a stall."}]
    return []


def stall_message_id(kind: str, stall: str, since: str, seq: int) -> str:
    """The courier id of one stall's raise or clear. `seq` is how many times that kind of row was already told for the
    stall (`told_counts`): a retry of a failed write gets the same id, which the inbox folds into one (last line per id
    wins), and a stall raised again after it cleared gets a new id, with no acks carried over."""
    return "stall-" + hashlib.sha1(f"{kind}|{stall}|{since}|{seq}".encode()).hexdigest()[:24]


def notice_word(stall: str, raised: bool) -> str:
    """How a stall's courier entry and push open: `no worker capacity` for that notice, else `stall` or `all clear`."""
    if stall.partition(":")[0] == NO_CAPACITY:
        return "no worker capacity"
    return "stall" if raised else "all clear"


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
    """The stalls standing now, in a fixed order: no land, no launch, each stranded task, the idle spell, then each
    repeated failure. Under the hard stop only the repeated failures stand; while the chair's idle-lanes line is raised,
    no launch does not."""
    found = [] if gate["paused"] else [
        no_land(facts, gate["held"], now),
        None if gate["idle_raised"] else no_launch(facts, gate["free_lanes"], gate["ready"], gate["subject"], now),
    ]
    held = [] if gate["paused"] else stranded(facts, now)
    return [*(s for s in found if s is not None), *held, *idle_spell(facts, gate, now), *repeats(facts, now)]


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


def plan_alarms(standing: Sequence[Stall], facts: StallFacts, now: datetime, paused: bool = False) -> list[Action]:
    """One stall_alarm per standing stall not raised already, then for each raised stall no longer standing one
    stall_clear: the all-clear, or, for a same-failure stall that only went quiet, a `quiet` one that closes it unsaid.
    An idle spell's alarm closes quietly too: the operator's own release ended it, and telling them so is noise.
    Under the hard stop (`paused`) a raised no-land, no-launch or idle stall is not judged, so it is neither cleared nor
    raised. An idle_held raise carries its held items. Each action carries `seq`, how many rows of its kind the stall
    was told in already (`told_counts`), which keeps its courier id its own (`stall_message_id`)."""
    keys = {s["key"] for s in standing}
    opened = facts["open"]
    told = facts.get("told") or {}
    from agent_tools.chair_types import ActionKind  # chair_types imports this module, so a top-level import is a cycle

    raised: list[Action] = []
    for s in standing:
        if s["key"] in opened:
            continue
        action: Action = {"kind": ActionKind.STALL_ALARM, "stall": s["key"], "since": s["since"], "subject": s["subject"],
                          "detail": s["detail"], "seq": told.get(f"{RAISE}|{s['key']}", 0)}
        if "held" in s:
            action["held"] = list(s["held"])
        raised.append(action)
    cleared: list[Action] = []
    for key, alarm in sorted(opened.items()):
        if key in keys:
            continue
        repeat = key.startswith(f"{REPEAT}:")
        if paused and not repeat:
            continue
        ending = repeat_ending(key, facts["heads"], now) if repeat else "quiet" if key.partition(":")[0] in IDLE_KEYS else "broken"
        if ending == "standing":
            continue
        quiet = ending == "quiet"
        clear: Action = {
            "kind": ActionKind.STALL_CLEAR, "stall": key, "since": alarm["since"], "subject": alarm["subject"],
            "detail": f"{'expired' if quiet else 'cleared'}: {alarm['detail']}", "seq": told.get(f"{CLEAR}|{key}", 0),
        }
        if quiet:
            clear["quiet"] = True
        cleared.append(clear)
    return [*raised, *cleared]


def is_busy(facts: Facts) -> bool:
    """A lane is live on this machine or a lane host, an initiative runs, or a land is in progress. Facts that carry no
    dispatch read as busy: the chair cannot tell idle from unknown, and an idle alarm on unknown lanes would be false."""
    dispatch = facts.get("dispatch")
    if not dispatch:
        return True
    hosts = dispatch.get("hosts") or []
    return (
        _count(dispatch.get("live_runs")) > 0
        or any(_count(h.get("live_runs")) > 0 for h in hosts)
        or bool(facts.get("running"))
        or bool(facts.get("landing"))
    )


def worker_capacity(
    dispatch: Mapping[str, object] | None, login_hosts: Collection[Mapping[str, object]], local_host: str
) -> int:
    """The lanes the chair could fill with epics, from the hosts placement uses. The local cap counts when this
    machine's own hosts-table row is `active` (or it has none, as a chair with no hosts table runs on its profile's cap)
    and its lanes are not kept for decomposes (`local_lanes`). A lane host counts its own capacity, else the local cap,
    when its row is `active`, or when it has no row, as a host named only by the profile. 0 means every host is
    drained or none is configured."""
    if not dispatch:
        return 0
    states = {str(r.get("name") or ""): str(r.get("state") or "") for r in login_hosts}

    def counts(name: str) -> bool:
        return states.get(name, "active") == "active"

    cap = _count(dispatch.get("max_in_flight"))
    local = cap if counts(local_host) and dispatch.get("local_lanes", "any") != "decompose" else 0
    hosts = dispatch.get("hosts")
    remote = [h for h in hosts if is_mapping(h)] if is_list(hosts) else []
    return local + sum(_count(h.get("capacity", cap)) for h in remote if counts(str(h.get("name") or "")))


def _count(value: object) -> int:
    """`int(value or 0)` for a count read off the idle-stall inputs."""
    return int(value) if isinstance(value, int | float | str) and value else 0


def plan_stall_alarm(
    facts: Facts, planned: Collection[Action], now: datetime, *, launch_paused: bool = False
) -> list[Action]:
    """The tick's alarm actions from `facts["stall_alarm"]` (none without it), `facts["idle_stall"]`'s free lanes,
    ready count and open idle line, the held initiatives, and the tick's planned actions, which say whether the chair's
    own idle line goes out this tick. `launch_paused` is the hard stop. The idle check reads the lanes and lands in
    progress (`is_busy`), the worker capacity (`worker_capacity`), the store's holds and this tick's no-progress holds
    (`held_items`), and the idle-stall inputs' last progress."""
    alarm = facts.get("stall_alarm")
    if not alarm:
        return []
    idle: Mapping[str, object] = facts.get("idle_stall") or {}
    progress = idle.get("last_progress_at")
    gate: Gate = {
        "busy": is_busy(facts),
        "capacity": worker_capacity(facts.get("dispatch"), facts.get("login_hosts") or (), alarm.get("local_host") or ""),
        "held_items": held_items(alarm.get("holds") or [], facts.get("no_progress") or {}),
        "progress_at": str(progress) if progress else None,
        "free_lanes": _count(idle.get("free_lanes")),
        "ready": _count(idle.get("ready")) + _count(idle.get("queued")),
        "subject": next((str(i["id"]) for i in facts.get("initiatives") or [] if i.get("ready_tasks") and launchable_by_tick(i)), ""),
        "held": frozenset(facts.get("held_initiatives") or ()) | frozenset(facts.get("no_progress") or {}),
        "idle_raised": bool(idle.get("open_signature"))
        or any(a.get("kind") == "needs_chair" and a.get("cause") in IDLE_CAUSES for a in planned),
        "paused": launch_paused,
    }
    return plan_alarms(observe(alarm, gate, now), alarm, now, launch_paused)
