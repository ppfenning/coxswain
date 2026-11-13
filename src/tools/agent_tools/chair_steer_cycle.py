"""The pure rule that names a steer deadlock and drops the approval-only entries that cause it."""

from typing import TypedDict

from agent_tools.chair_steer import steer_check
from agent_tools.chair_types import Facts, InitiativeFacts, RunningInitiative


class Cycle(TypedDict):
    repo: str
    members: list[str]  # the deadlocked ready initiatives, in the planner's priority order


def _live(entry: RunningInitiative) -> bool:
    return entry.get("live", True)


def _ready(facts: Facts, repo: str) -> list[InitiativeFacts]:
    return [i for i in facts["initiatives"] if i.get("repo") == repo and i.get("ready_surfaces")]


def _cycle_in(facts: Facts, repo: str) -> Cycle | None:
    entries = [e for e in facts.get("running", []) if e["repo"] == repo]
    ready = _ready(facts, repo)
    if len(ready) < 2 or any(_live(e) for e in entries):
        return None
    policy = facts.get("steer_policy")
    if any(steer_check(i["id"], repo, i["ready_surfaces"], entries, {}, None, policy) is None for i in ready):
        return None
    return {"repo": repo, "members": [i["id"] for i in ready]}


def find_cycle(facts: Facts) -> Cycle | None:
    """The first repo, in order of its first ready initiative, where no run is live and every ready initiative is held by another's approvals."""
    repos = dict.fromkeys(i["repo"] for i in facts["initiatives"] if i.get("repo") and i.get("ready_surfaces"))
    return next((c for repo in repos if (c := _cycle_in(facts, repo)) is not None), None)


def break_cycle(facts: Facts) -> tuple[Facts, Cycle | None]:
    """Facts without the cycle repo's running entries, all non-live by construction; unchanged when there is no cycle."""
    cycle = find_cycle(facts)
    if cycle is None:
        return facts, None
    kept = [e for e in facts.get("running", []) if e["repo"] != cycle["repo"]]
    return {**facts, "running": kept}, cycle
