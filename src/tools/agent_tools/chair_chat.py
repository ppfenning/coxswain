"""Pure core of the chair conversation channel: chat messages, directives and answers over literal facts."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from functools import reduce
from typing import Literal, NotRequired, TypedDict

from agent_tools.courier import Reference, send

BodyKind = Literal["directive", "question"]
TaskState = Literal["ready", "approved", "done", "quarantined", "dropped", "building"]


class ViewTask(TypedDict):
    id: str
    state: TaskState


class ViewLevel(TypedDict):
    phase: str
    tasks: list[ViewTask]


class EpicView(TypedDict):
    """One initiative as an answer reads it: its tasks by phase, and whether its last run has exited."""

    epic_id: str
    levels: list[ViewLevel]
    exited: bool


class PlannedAction(TypedDict):
    """One planned action as an answer reads it: its kind, the initiative as topic, and its scalar fields as text."""

    kind: str
    topic: str
    fields: dict[str, str]
    key: str
    event: NotRequired[str]


def event_key(event: str, fields: dict[str, str]) -> str:
    """Stable key of one event: its name plus its field values by field name."""
    return ":".join([event, *(value for _, value in sorted(fields.items()))])

LABEL = "chair_chat"
DIRECTIVE_KINDS = ("priority", "hold", "release", "scope", "feature", "cut_release")
QUESTION_KINDS = ("left_on_release", "why_not_launched", "what_failed")

_EVENT_FOR: dict[str, str] = {"directive": "chat_directive", "question": "chat_question"}
_BODY_FOR: dict[str, BodyKind] = {"chat_directive": "directive", "chat_question": "question"}
_TARGET_KINDS = ("initiative", "task")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_LEFT = ("ready", "building", "quarantined")
_FAILURE_EVENTS = ("needs_attention", "stuck", "gate_failed", "merge_failed")


class ChatMessage(TypedDict):
    id: str
    sender: str
    body_kind: BodyKind
    payload: dict[str, str]


class DirectiveState(TypedDict):
    applied: list[str]
    priorities: dict[str, str]  # "task:t1" to its rank
    holds: list[str]  # "initiative:i1" entries, sorted
    scope: list[str]  # ["all"] or the initiative ids the chair may work
    features: dict[str, str]  # feature name to "on" or "off"
    cut_release: str  # the version asked for, "" when none


Event = dict[str, object]


def empty_state() -> DirectiveState:
    return {"applied": [], "priorities": {}, "holds": [], "scope": ["all"], "features": {}, "cut_release": ""}


# A chat message rides a courier entry: topic is the label, event is chat_directive or chat_question,
# and the flat string payload is the entry's `fields`. courier.send validates the event name.
def build_message(sender: str, body_kind: BodyKind, payload: dict[str, str], message_id: str, ref: Reference) -> dict:
    note = f"{body_kind}: {payload.get('kind', '')}"
    return send(ref, sender, "chair", note, message_id, event=_EVENT_FOR[body_kind], fields=payload, topic=LABEL)


def _text(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _strings(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


def parse_message(entry: object) -> ChatMessage | None:
    """The chat message in a courier entry, or None when the entry is not a well-formed one."""
    if not isinstance(entry, dict):
        return None
    event, sender, id_, fields = entry.get("event"), entry.get("from"), entry.get("id"), entry.get("fields")
    body_kind = _BODY_FOR.get(event) if isinstance(event, str) else None
    well_formed = entry.get("topic") == LABEL and body_kind is not None and _text(sender) and _text(id_)
    if well_formed and _strings(fields):
        return {"id": str(id_), "sender": str(sender), "body_kind": body_kind, "payload": dict(fields)}
    return None


def _get(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _refusal(checks: Sequence[tuple[bool, str]]) -> str | None:
    return next((reason for ok, reason in checks if not ok), None)


def _has_target(p: dict) -> list[tuple[bool, str]]:
    return [
        (_get(p, "target_kind") in _TARGET_KINDS, "target_kind must be initiative or task"),
        (_get(p, "target") != "", "target is required"),
    ]


def _rank_ok(p: dict) -> bool:
    rank = _get(p, "rank")
    return rank.isascii() and rank.isdigit() and int(rank) >= 1


def _scope_ids(p: dict) -> list[str]:
    return [part.strip() for part in _get(p, "initiatives").split(",")]


def _scope_ok(p: dict) -> bool:
    ids = _scope_ids(p)
    return all(ids) and ("all" not in ids or ids == ["all"])


_CHECKS: dict[str, Callable[[dict], str | None]] = {
    "priority": lambda p: _refusal([*_has_target(p), (_rank_ok(p), "rank must be a whole number of 1 or more")]),
    "hold": lambda p: _refusal(_has_target(p)),
    "release": lambda p: _refusal(_has_target(p)),
    "scope": lambda p: _refusal([(_scope_ok(p), "initiatives must be all or a comma list of initiative ids")]),
    "feature": lambda p: _refusal([
        (_get(p, "name") != "", "feature name is required"),
        (_get(p, "state") in ("on", "off"), "feature state must be on or off"),
    ]),
    "cut_release": lambda p: _refusal([(_VERSION.match(_get(p, "version")) is not None, "version must look like 1.2.3")]),
}


def validate_directive(kind: object, payload: object) -> str | None:
    """None when the directive is well formed, else the reason it is refused. Never raises."""
    if not isinstance(payload, dict):
        return "payload must be a mapping"
    check = _CHECKS.get(kind) if isinstance(kind, str) else None
    return f"unknown directive kind {kind!r}" if check is None else check(payload)


def _target(p: dict) -> str:
    return f"{_get(p, 'target_kind')}:{_get(p, 'target')}"


_EFFECTS: dict[str, Callable[[DirectiveState, dict], dict]] = {
    "priority": lambda s, p: {"priorities": {**s["priorities"], _target(p): _get(p, "rank")}},
    "hold": lambda s, p: {"holds": sorted({*s["holds"], _target(p)})},
    "release": lambda s, p: {"holds": [h for h in s["holds"] if h != _target(p)]},
    "scope": lambda s, p: {"scope": _scope_ids(p)},
    "feature": lambda s, p: {"features": {**s["features"], _get(p, "name"): _get(p, "state")}},
    "cut_release": lambda s, p: {"cut_release": _get(p, "version")},
}


def _event(name: str, fields: dict[str, str]) -> Event:
    return {"kind": "publish", "topic": LABEL, "event": name, "fields": fields, "key": event_key(name, fields)}


def _step(now: str, acc: tuple[DirectiveState, list[Event]], entry: object) -> tuple[DirectiveState, list[Event]]:
    state, events = acc
    message = parse_message(entry)
    if message is None or message["body_kind"] != "directive" or message["id"] in state["applied"]:
        return acc
    payload = message["payload"]
    kind = payload.get("kind", "")
    base = {**payload, "id": message["id"], "kind": kind, "sender": message["sender"], "at": now}
    reason = validate_directive(kind, payload)
    if reason is None:
        new = {**state, **_EFFECTS[kind](state, payload), "applied": [*state["applied"], message["id"]]}
        return new, [*events, _event("directive_applied", base)]
    return state, [*events, _event("directive_refused", {**base, "reason": reason})]


def apply_directives(
    state: DirectiveState, entries: Sequence[object], now: str
) -> tuple[DirectiveState, list[Event]]:
    """A new state and one publish event per directive. An id applies once; a refused id is not recorded."""
    return reduce(lambda acc, entry: _step(now, acc, entry), entries, (state, []))


def _unnamed(subject: str) -> str:
    return f"The facts do not name {subject or 'a subject'}."


def _describe(action: PlannedAction) -> str:
    return f"{action['kind']}({', '.join(f'{k}={v}' for k, v in sorted(action['fields'].items()))})"


def _planned(epic_id: str, plan: Sequence[PlannedAction]) -> str:
    mine = [a for a in plan if a["topic"] == epic_id]
    return " Planned: " + ", ".join(_describe(a) for a in mine) + "." if mine else " Nothing is planned."


def _tasks(facts: EpicView) -> list[dict]:
    return [t for level in facts["levels"] for t in level["tasks"]]


def _left_on_release(subject: str, facts: Sequence[EpicView], plan: Sequence[PlannedAction]) -> str:
    epic = next((f for f in facts if f["epic_id"] == subject), None)
    if epic is None:
        return _unnamed(subject)
    left = [t for t in _tasks(epic) if t["state"] in _LEFT]
    head = (
        f"{subject}: {len(left)} left: " + ", ".join(f"{t['id']} ({t['state']})" for t in left) + "."
        if left
        else f"{subject}: nothing is left; every task is approved, done or dropped."
    )
    return head + _planned(subject, plan)


def _why_task(epic: EpicView, task: dict, plan: Sequence[PlannedAction]) -> str:
    relaunch = any(a["topic"] == epic["epic_id"] and a["kind"] == "relaunch" for a in plan)
    state = task["state"]
    name = task["id"]
    if state == "ready" and relaunch:
        return f"{name} is ready and a relaunch is planned for {epic['epic_id']}."
    if state == "ready":
        cause = "no planned action launches it" if epic["exited"] else f"the run for {epic['epic_id']} has not exited"
        return f"{name} is ready but not launched: {cause}."
    if state == "quarantined":
        return f"{name} is quarantined and needs attention before it can launch."
    if state == "building":
        return f"{name} is already building."
    return f"{name} is {state}, so there is nothing to launch."


def _why_not_launched(subject: str, facts: Sequence[EpicView], plan: Sequence[PlannedAction]) -> str:
    task_hit = next(((f, t) for f in facts for t in _tasks(f) if t["id"] == subject), None)
    epic = next((f for f in facts if f["epic_id"] == subject), None)
    if task_hit is not None:
        return _why_task(task_hit[0], task_hit[1], plan)
    if epic is not None:
        counts = {s: sum(1 for t in _tasks(epic) if t["state"] == s) for s in sorted({t["state"] for t in _tasks(epic)})}
        shown = ", ".join(f"{n} {s}" for s, n in counts.items()) or "no tasks"
        return f"{subject} has {shown}; exited is {str(epic['exited']).lower()}." + _planned(subject, plan)
    return _unnamed(subject)


def _what_failed(_subject: str, facts: Sequence[EpicView], plan: Sequence[PlannedAction]) -> str:
    quarantined = [f"{t['id']} on {f['epic_id']}" for f in facts for t in _tasks(f) if t["state"] == "quarantined"]
    failures = [_describe(a) for a in plan if a.get("event") in _FAILURE_EVENTS]
    lines = [
        "Quarantined: " + ", ".join(quarantined) + "." if quarantined else "Nothing is quarantined.",
        "Failure events planned: " + ", ".join(failures) + "." if failures else "No failure events are planned.",
    ]
    return " ".join(lines)


_ANSWERS: dict[str, Callable[[str, Sequence[EpicView], Sequence[PlannedAction]], str]] = {
    "left_on_release": _left_on_release,
    "why_not_launched": _why_not_launched,
    "what_failed": _what_failed,
}


def answer(question: dict[str, str], facts: Sequence[EpicView], plan: Sequence[PlannedAction]) -> str:
    """Text built only from the facts and planned actions given. `question` carries `kind` and `subject`."""
    kind = question.get("kind", "")
    handler = _ANSWERS.get(kind)
    return f"Unknown question kind {kind!r}." if handler is None else handler(question.get("subject", ""), facts, plan)


def state_summary(state: DirectiveState, facts: Sequence[EpicView], recent_events: Sequence[Event]) -> str:
    """The text a session shows at start: directives in force, the epics in the facts, and recent events."""
    holds = ", ".join(state["holds"]) or "none"
    features = ", ".join(f"{k}={v}" for k, v in sorted(state["features"].items())) or "none"
    epics = ", ".join(f"{f['epic_id']} ({sum(1 for t in _tasks(f) if t['state'] in _LEFT)} left)" for f in facts) or "none"
    recent = ", ".join(str(e.get("event", "?")) for e in recent_events[-5:]) or "none"
    return "\n".join([
        f"Directives applied: {len(state['applied'])}",
        f"Scope: {', '.join(state['scope'])}",
        f"Holds: {holds}",
        f"Features: {features}",
        f"Cut release: {state['cut_release'] or 'none'}",
        f"Epics: {epics}",
        f"Recent events: {recent}",
    ])
