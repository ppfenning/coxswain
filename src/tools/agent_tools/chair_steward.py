"""Pure steward decisions for an epic's routine steps, over literal facts."""

from collections.abc import Collection, Sequence
from typing import Literal, NotRequired, TypedDict

TaskState = Literal["ready", "approved", "done", "quarantined", "dropped", "building"]
ActionKind = Literal["merge_level", "relaunch", "run_gate", "publish"]

_SETTLED = ("approved", "done")
_OPEN = ("ready", "building", "quarantined")


class StewardTask(TypedDict):
    id: str
    state: TaskState


class StewardLevel(TypedDict):
    tasks: list[StewardTask]
    merged: bool


class StewardFacts(TypedDict):
    epic_id: str
    branch_head: str
    levels: list[StewardLevel]
    exited: bool
    signature: str
    reported: Collection[str]


class StewardAction(TypedDict):
    kind: ActionKind
    topic: str
    fields: dict[str, str]
    key: str
    event: NotRequired[str]


def event_key(event: str, fields: dict[str, str]) -> str:
    """Stable reported-set key: the event plus its field values by name, so each action is keyed by its own scope."""
    return ":".join([event, *(value for _, value in sorted(fields.items()))])


def _action(facts: StewardFacts, kind: ActionKind, event: str, fields: dict[str, str]) -> StewardAction:
    return {"kind": kind, "topic": facts["epic_id"], "fields": fields, "key": event_key(event, fields)}


def _publish(facts: StewardFacts, event: str, fields: dict[str, str]) -> StewardAction:
    return {**_action(facts, "publish", event, fields), "event": event}


def _live(level: StewardLevel) -> list[StewardTask]:
    return [t for t in level["tasks"] if t["state"] != "dropped"]


def epic_actions(facts: StewardFacts) -> list[StewardAction]:
    """Ordered actions for one epic: attention, then a merge, else a relaunch or stuck, else the gate."""
    levels = facts["levels"]
    if not any(_live(lv) for lv in levels):
        return []
    reported = facts["reported"]
    states = {t["state"] for lv in levels for t in lv["tasks"]}
    quarantined = [t for lv in levels for t in lv["tasks"] if t["state"] == "quarantined"]
    attention = [
        a
        for a in (_publish(facts, "needs_attention", {"task": t["id"]}) for t in quarantined)
        if a["key"] not in reported
    ]
    unmerged = [(i, _live(lv)) for i, lv in enumerate(levels) if not lv["merged"] and _live(lv)]
    if unmerged and all(t["state"] in _SETTLED for t in unmerged[0][1]):
        merge = _action(
            facts, "merge_level", "level_merged", {"head": facts["branch_head"], "level": str(unmerged[0][0])}
        )
        return [*attention, merge]
    if unmerged or states & set(_OPEN):
        return [*attention, *_relaunch_or_stuck(facts, blocked=bool(quarantined), ready="ready" in states)]
    return [*attention, *_gate(facts)]


def _relaunch_or_stuck(facts: StewardFacts, *, blocked: bool, ready: bool) -> list[StewardAction]:
    if blocked or not facts["exited"] or not ready:
        return []
    fields = {"signature": facts["signature"]}
    if event_key("relaunched", fields) not in facts["reported"]:
        return [_action(facts, "relaunch", "relaunched", fields)]
    stuck = _publish(facts, "stuck", fields)
    return [] if stuck["key"] in facts["reported"] else [stuck]


def _gate(facts: StewardFacts) -> list[StewardAction]:
    """Gate keys are per branch head: run_gate once, then epic_complete once after gate_passed."""
    fields = {"head": facts["branch_head"]}
    reported = facts["reported"]
    if event_key("gate_passed", fields) in reported:
        done = _publish(facts, "epic_complete", fields)
        return [] if done["key"] in reported else [done]
    gate = _action(facts, "run_gate", "run_gate", fields)
    return [] if gate["key"] in reported else [gate]


def decide(epics: Sequence[StewardFacts]) -> list[StewardAction]:
    """Actions for every epic in order, with at most one run_gate so gates run serially."""
    actions = [a for facts in epics for a in epic_actions(facts)]
    first_gate = next((i for i, a in enumerate(actions) if a["kind"] == "run_gate"), None)
    return [a for i, a in enumerate(actions) if a["kind"] != "run_gate" or i == first_gate]
