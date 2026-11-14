"""Plan the recovery half of a chair tick: relaunches, harness retries, rescues and needs-the-chair reports.

Pure. Takes the facts, returns actions. No pacing, no run history, no I/O.
"""
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from functools import reduce
from typing import Literal, cast

from agent_tools import chair_sort
from agent_tools.chair_launch_budget import DEFAULT_MAX_LAUNCHES_PER_HOUR, Launch, RunOutcome, Verdict, launch_budget
from agent_tools.chair_read_quarantined import RUNAWAY_CAUSE
from agent_tools.chair_remedy import remedy_for, trim_reason
from agent_tools.chair_revert import revert_stopped
from agent_tools.chair_steer import steer_check
from agent_tools.chair_types import (
    DEFAULT_QUARANTINE_HOLD_AFTER,
    QUARANTINED_CAUSE,
    Action,
    Facts,
    HeldTask,
    InitiativeFacts,
    QuarantineFacts,
    ReadyTask,
    RunningInitiative,
    SteerPolicy,
)
from agent_tools.run_death_cause import SCHEMA_VERSION_CAUSE

Recovery = Literal["rescue", "retry", "needs_chair", "relaunch", "triage", "hold", "none"]

STRANDED_CAUSE = "stranded"
MAIN_RED_CAUSE = "main_red"
# A ticket or code quarantine that reaches the chair names its first and newest reason.
TRIAGED_CAUSES = ("ticket", "code")
# A failure that needs a tweak, not a stoppage: the model triages it from the record and the whole initiative before
# any rebuild. "quarantined" is an open quarantine whose attempts name no cause, which is as unknown as "unknown".
TWEAK_CAUSES = ("code", "review", "ticket", "unknown", QUARANTINED_CAUSE)
_HALF_REASON = 300


def _recovery(
    q: QuarantineFacts,
    approved: set[tuple[str, str]],
    outcomes: Mapping[str, list[str]],
    hold_after: int = DEFAULT_QUARANTINE_HOLD_AFTER,
) -> Recovery:
    # A runaway-ceiling quarantine always reaches the chair, ahead of every other rule: no harness_failures
    # count, retry count or approval state ever turns it into a retry or a rescue.
    if q["cause"] == RUNAWAY_CAUSE:
        return "needs_chair"
    # A stranded task that is also an approved, unlanded row waits for its own phase to land as a whole: no
    # lone land, and not the chair either. The match is (initiative, task id), not the bare id. It cannot
    # include phase: a task id repeats across phases and QuarantineFacts carries no phase, so a same-id task in
    # another phase of the same initiative still matches.
    if q["cause"] == STRANDED_CAUSE and (q["initiative"], q["task_id"]) in approved:
        return "none"
    # A main_red quarantine recovers by the ordinary relaunch pair until two reverts in a row. Then it plans
    # nothing and still blocks its initiative (_blocks): relaunching onto a main that keeps going red stacks work on it.
    if q["cause"] == MAIN_RED_CAUSE:
        return "none" if revert_stopped(outcomes.get(q["initiative"], [])) else "relaunch"
    # A failure that needs a tweak is never rebuilt as it was: the next run triages it first. A fact that names no run
    # has no attempt for triage to read and no state to ask once for, so it reaches the chair. At the hold limit the
    # task waits for a person.
    if q["cause"] in TWEAK_CAUSES:
        if not q.get("run"):
            return "needs_chair"
        return "hold" if 0 < hold_after <= q.get("failures", 0) else "triage"
    # harness_failures == 0 on a harness cause is not a case the rules name; the chair looks at it.
    if q["cause"] != "harness" or q["harness_failures"] != 1:
        return "needs_chair"
    if q["rescue_failed"]:
        return "needs_chair"
    if q["has_patch"]:
        return "rescue"
    return "retry"


def _first_cause(q: QuarantineFacts) -> str:
    """The oldest attempt's cause on the current body; the fact's own cause when it lists none."""
    causes = q.get("causes") or []
    return causes[0] if causes else q["cause"]


def _reason_text(q: QuarantineFacts) -> str:
    """The first and newest attempt's reason for a ticket or code quarantine with two or more, each cut to 300 characters;
    the newest reason cut to 600 otherwise. `reasons` is every attempt's reason on the current body, oldest first."""
    reasons: Sequence[str] = cast(Mapping[str, Sequence[str]], q).get("reasons") or []
    if q["cause"] in TRIAGED_CAUSES and len(reasons) >= 2:
        return f"first: {reasons[0].strip()[:_HALF_REASON]} | latest: {reasons[-1].strip()[:_HALF_REASON]}"
    return trim_reason(q.get("reason"))


def _quarantine_action(q: QuarantineFacts, kind: Recovery) -> Action:
    if kind == "needs_chair" and "reason" not in q:
        # A fact that carries no row reason keeps the old shape: there is nothing to record beyond the cause.
        return {"kind": "needs_chair", "initiative": q["initiative"], "task_id": q["task_id"], "cause": q["cause"]}
    if kind == "needs_chair":
        cause = _first_cause(q)
        remedy = remedy_for(cause, q.get("run", ""), q["task_id"], q.get("repo", ""))
        return {
            "kind": "needs_chair",
            "initiative": q["initiative"],
            "task_id": q["task_id"],
            "cause": cause,
            "reason": _reason_text(q),
            "remedy": remedy["kind"],
            "command": remedy["command"],
        }
    return {"kind": kind, "task_id": q["task_id"], "initiative": q["initiative"]}


def _blocks(q: QuarantineFacts, kind: Recovery) -> bool:
    """A "relaunch" recovery leaves the relaunch pair to _relaunch_actions; a stopped main_red keeps blocking it.

    A triage request or a hold blocks nothing: the initiative's other ready work relaunches, and that run triages the task."""
    return False if kind in ("relaunch", "triage", "hold") else kind != "none" or q["cause"] == MAIN_RED_CAUSE


def _hold_reason(q: QuarantineFacts, kind: Recovery, hold_after: int) -> str:
    """Why a person must decide, with the mark: the count reached the limit, or a requested triage left the task quarantined."""
    from core.workstore import hold_text  # lazy: core sits beside agent_tools in one venv

    newest = trim_reason(q.get("reason")) or "no reason recorded"
    if kind == "hold":
        why = f"failed {q.get('failures', 0)} times (chair.quarantine_hold_after is {hold_after}), so it is not triaged again"
    else:
        why = f"triage was asked for after run {q.get('run') or '(unknown)'} and the task is quarantined again in that state"
    return hold_text(f"{why}; newest: {newest}")


def _tweak_actions(q: QuarantineFacts, kind: Recovery, handled: Collection[str], hold_after: int) -> list[Action]:
    """A failure that needs a tweak: one triage request per task state, else one hold per state, else the chair."""
    run = q.get("run", "")
    base: Action = {"task_id": q["task_id"], "initiative": q["initiative"], "cause": q["cause"]}
    if kind == "triage" and (key := chair_sort.triage_key(q["task_id"], run)) not in handled:
        return [{**base, "kind": "request_triage", "key": key, "reason": trim_reason(q.get("reason"))}]
    hold = chair_sort.hold_key(q["task_id"], run)
    if hold in handled:
        return [_quarantine_action(q, "needs_chair")]
    return [{**base, "kind": "hold_task", "key": hold, "reason": _hold_reason(q, kind, hold_after)}]


def _rescue_actions(q: QuarantineFacts, handled: Collection[str]) -> list[Action]:
    """One rescue per task state (`rescue:<task>:<run>`). A rescue already launched for this state that left the task
    quarantined in it (a rescue that found the task not eligible writes nothing) reaches the chair, never goes out again."""
    key = chair_sort.rescue_key(q["task_id"], q.get("run", ""))
    if key in handled:
        return [_quarantine_action(q, "needs_chair")]
    return [{"kind": "rescue", "initiative": q["initiative"], "task_id": q["task_id"], "key": key}]


def _sorted_action(q: QuarantineFacts, handled: Collection[str], recovery: Recovery) -> list[Action] | None:
    """chair_sort's answer for a harness quarantine: its actions, [] for a skip. None when the sort does not name it.

    A harness quarantine is named by its `task_state_key`, and only while the existing rules would retry it: a patch to
    rescue, a failed rescue or a repeated failure stay as they were. A review or code quarantine is no longer sorted
    here: it goes to the model's triage (`_tweak_actions`)."""
    if q["cause"] == chair_sort.HARNESS_CAUSE and "task_state_key" in q and recovery == "retry":
        keyed, state = q, q["task_state_key"]
    else:
        return None
    base: Action = {"task_id": q["task_id"], "initiative": q["initiative"]}
    if chair_sort.classify(keyed, state, handled) == "rebuild_on_main":
        return [{**base, "kind": "rebuild_quarantined", "key": chair_sort.state_key(q["task_id"], state)}]
    return []


def _quarantine_actions(
    quarantines: list[QuarantineFacts],
    approved: set[tuple[str, str]],
    outcomes: Mapping[str, list[str]],
    handled: Collection[str] = (),
    hold_after: int = DEFAULT_QUARANTINE_HOLD_AFTER,
) -> list[Action]:
    kinds = {id(q): _recovery(q, approved, outcomes, hold_after) for q in quarantines}
    named = {
        id(q): _tweak_actions(q, kinds[id(q)], handled, hold_after) if kinds[id(q)] in ("triage", "hold")
        else _rescue_actions(q, handled) if kinds[id(q)] == "rescue"
        else _sorted_action(q, handled, kinds[id(q)])
        for q in quarantines
    }
    # A sorted needs_chair holds back the initiative's other retries exactly as an unsorted one does.
    chair = {
        q["initiative"]
        for q in quarantines
        if (kinds[id(q)] == "needs_chair" and named[id(q)] is None)
        or any(a["kind"] == "needs_chair" for a in named[id(q)] or [])
    }

    def plain(q: QuarantineFacts) -> list[Action]:
        kind = kinds[id(q)]
        emitted = kind not in ("none", "relaunch") and (kind == "needs_chair" or q["initiative"] not in chair)
        return [_quarantine_action(q, kind)] if emitted else []

    return [a for q in quarantines for a in (named[id(q)] if named[id(q)] is not None else plain(q))]


def _first_unmet_need(t: ReadyTask, landed: set[str]) -> str | None:
    """The first id in `t["needs"]` not in `landed`, in needs order; None when every need is landed."""
    return next((need for need in t["needs"] if need not in landed), None)


def _ready_and_met(t: ReadyTask, landed: set[str]) -> bool:
    return _first_unmet_need(t, landed) is None


def _initiative_first_unmet_need(i: InitiativeFacts) -> str | None:
    """None when a ready task has every need landed; else the first unmet need, ready tasks before waiting ones."""
    if any(_ready_and_met(t, i["landed"]) for t in i["ready_tasks"]):
        return None
    candidates = [*i["ready_tasks"], *i.get("waiting_tasks", [])]
    return next((n for t in candidates if (n := _first_unmet_need(t, i["landed"])) is not None), None)


def _can_relaunch(i: InitiativeFacts, blocked: set[str]) -> bool:
    return (
        i["started"]
        and not i.get("dag_problem")
        and i["id"] not in blocked
        and bool(i["ready_tasks"])
        and _initiative_first_unmet_need(i) is None
    )


def relaunch_pair(initiative: str) -> list[Action]:
    """The clear_branches then relaunch pair a healthy relaunch emits for one initiative."""
    return [{"kind": "clear_branches", "initiative": initiative}, {"kind": "relaunch", "initiative": initiative}]


def landing_initiatives(facts: Facts) -> frozenset[str]:
    """The initiatives a land in flight touches: the one it names. A land naming no initiative touches its whole repo.

    Other initiatives in a landing repo stay launchable: a build on a phase branch never waits on a land to main,
    and the stale-base rebase handles a base the land moved meanwhile."""
    landings = facts.get("landing", [])
    named = frozenset(landing["initiative"] for landing in landings if landing.get("initiative"))
    whole_repos = frozenset(landing["repo"] for landing in landings if not landing.get("initiative"))
    return named | frozenset(i["id"] for i in facts["initiatives"] if i.get("repo") in whole_repos)


def _launch(record: Mapping[str, str]) -> Launch | None:
    """None for a record whose timestamp is missing or unparseable. A naive timestamp is read as UTC, so a
    `datetime.utcnow().isoformat()` record still counts against the budget instead of silently not counting."""
    try:
        at = datetime.fromisoformat(record["at"])
    except (KeyError, ValueError, TypeError):
        return None
    return Launch(at if at.tzinfo is not None else at.replace(tzinfo=UTC), record.get("kind", ""))


def budget_verdict(facts: Facts, initiative: str, kind: str, now: datetime) -> Verdict:
    """launch_budget over the initiative's launch_history entry; every absent key reads as empty or the default."""
    entry = facts.get("launch_history", {}).get(initiative, {})
    launches = [launch for record in entry.get("launches", []) if (launch := _launch(record)) is not None]
    outcomes = [
        RunOutcome(
            run.get("run_id", ""),
            run.get("quarantined", False),
            run.get("reason", ""),
            run.get("body", ""),
            run.get("main_head", ""),
        )
        for run in entry.get("runs", [])
    ]
    limit = facts.get("max_launches_per_hour", DEFAULT_MAX_LAUNCHES_PER_HOUR)
    return launch_budget(now, kind, launches, outcomes, entry.get("body", ""), entry.get("main_head", ""), limit)


def _relaunch_candidates(facts: Facts, blocked: set[str]) -> list[InitiativeFacts]:
    landing = landing_initiatives(facts)
    return [i for i in facts["initiatives"] if _can_relaunch(i, blocked) and i["id"] not in landing]


def _lane_capacity_zero(facts: Facts) -> bool:
    """True when the local machine has no lanes and every host row is capped at 0; a row with no capacity is uncapped."""
    dispatch = facts["dispatch"]
    return dispatch["max_in_flight"] == 0 and all(h.get("capacity") == 0 for h in dispatch.get("hosts", []))


NO_CAPACITY = Verdict(allowed=False, reason="no lane capacity: the local machine and every host are at 0", kind="capacity")


def refused_relaunches(facts: Facts, now: datetime | None, blocked: set[str]) -> dict[str, Verdict]:
    """Each initiative that would relaunch, to the verdict that refuses it: lane capacity zero, the budget or the loop rule.

    Empty when now is None."""
    if now is None:
        return {}
    candidates = _relaunch_candidates(facts, blocked)
    if _lane_capacity_zero(facts):
        return {i["id"]: NO_CAPACITY for i in candidates}
    verdicts = {i["id"]: budget_verdict(facts, i["id"], "relaunch", now) for i in candidates}
    return {initiative: v for initiative, v in verdicts.items() if not v.allowed}


def _loop_key(verdict: Verdict) -> str:
    return "|".join(verdict.run_ids)


def _relaunch_actions(
    initiatives: list[InitiativeFacts],
    blocked: set[str],
    landing: frozenset[str],
    refused: Mapping[str, Verdict] | None = None,
    reported: Collection[str] = (),
) -> list[Action]:
    """The relaunch pair per relaunchable initiative. A refused one plans no pair; a relaunch_loop refusal plans
    one needs_chair naming both runs, unless its `<run1>|<run2>` key is already in `reported`."""
    refusals = {} if refused is None else refused
    return [
        action
        for i in initiatives
        if _can_relaunch(i, blocked) and i["id"] not in landing
        for action in _relaunch_or_refusal(i["id"], refusals.get(i["id"]), reported)
    ]


def _relaunch_or_refusal(initiative: str, refusal: Verdict | None, reported: Collection[str]) -> list[Action]:
    if refusal is None:
        return relaunch_pair(initiative)
    if refusal.kind != "relaunch_loop" or _loop_key(refusal) in reported:
        return []
    return [{"kind": "needs_chair", "initiative": initiative, "cause": "relaunch_loop", "reason": refusal.reason}]


def _waiting_actions(initiatives: list[InitiativeFacts], blocked: set[str]) -> list[Action]:
    """One `waiting on <need>` needs_chair per started initiative with no quarantine of its own and an unmet need."""
    return [
        {"kind": "needs_chair", "initiative": i["id"], "cause": f"waiting on {need}"}
        for i in initiatives
        if i["started"]
        and not i.get("dag_problem")
        and i["id"] not in blocked
        and (need := _initiative_first_unmet_need(i)) is not None
    ]


def _invalid_dag_actions(initiatives: list[InitiativeFacts]) -> list[Action]:
    """One `invalid_dag (<problem>)` needs_chair per initiative whose task DAG is invalid, started or not."""
    return [
        {"kind": "needs_chair", "initiative": i["id"], "cause": f"invalid_dag ({i['dag_problem']})"}
        for i in initiatives
        if i.get("dag_problem")
    ]


def _schema_death_actions(deaths: dict[str, list[str]]) -> list[Action]:
    """One needs_chair per initiative whose newest two runs both died on the schema-version refusal."""
    return [
        {
            "kind": "needs_chair",
            "initiative": i,
            "cause": SCHEMA_VERSION_CAUSE,
            "reason": f"the lane host's graphs is older than the shared store; runs {' and '.join(runs)} both died on a schema-version refusal",
        }
        for i, runs in deaths.items()
    ]


def _without_schema_dead_launches(actions: list[Action], deaths: dict[str, list[str]]) -> list[Action]:
    """Drop a retry or rescue for a schema-dead initiative: both start a run on the stale graphs."""
    return [a for a in actions if not (a["kind"] in {"retry", "rescue"} and a["initiative"] in deaths)]


_LAUNCHES = {"relaunch", "retry", "rescue"}


def _without_over_budget(actions: list[Action], over: Collection[str]) -> list[Action]:
    """Drop a retry or rescue for an initiative the launch budget refuses; a needs_chair stays."""
    return [a for a in actions if not (a["kind"] in _RETRIES and a["initiative"] in over)]


def _entry(i: InitiativeFacts) -> RunningInitiative | None:
    """The initiative as a running entry; None when its repo is unknown, so there is nothing to compare."""
    repo = i.get("repo")
    return None if repo is None else {"id": i["id"], "repo": repo, "surfaces": i.get("ready_surfaces", [])}


def _steer(
    a: Action,
    by_id: dict[str, InitiativeFacts],
    running: list[RunningInitiative],
    streaks: Mapping[str, int],
    policy: SteerPolicy | None,
) -> tuple[Action, RunningInitiative | None]:
    """The action to return for a launch, and the running entry it claims; a deferral claims nothing."""
    i = by_id.get(a["initiative"])
    entry = None if i is None else _entry(i)
    if entry is None:
        return a, None
    deferred = steer_check(entry["id"], entry["repo"], entry["surfaces"], running, streaks, policy=policy)
    return (a, entry) if deferred is None else (deferred, None)


def _without_deferred_clears(before: list[Action], after: list[Action]) -> list[Action]:
    """A deferred relaunch takes its paired clear_branches with it: the clear prunes epic/<initiative>/* branches."""
    deferred = {a["initiative"] for a in before if a["kind"] == "relaunch"} - {
        a["initiative"] for a in after if a["kind"] == "relaunch"
    }
    return [a for a in after if not (a["kind"] == "clear_branches" and a["initiative"] in deferred)]


def _steered(actions: list[Action], facts: Facts) -> list[Action]:
    """Replace each launch overlapping a running or earlier-kept initiative with its steer_check action."""
    # A retry or rescue is checked on the initiative's ready_surfaces, as the ticket specifies; the quarantined
    # task's own surfaces are not in facts. plan_lost_runs relaunches are not checked here: a follow-up item.
    by_id = {i["id"]: i for i in facts["initiatives"]}
    base = facts.get("running", [])
    streaks = facts.get("steer_streaks", {})
    policy = facts.get("steer_policy")

    def fold(
        state: tuple[list[Action], list[RunningInitiative]], a: Action
    ) -> tuple[list[Action], list[RunningInitiative]]:
        out, kept = state
        if a["kind"] not in _LAUNCHES:
            return [*out, a], kept
        action, entry = _steer(a, by_id, [*base, *kept], streaks, policy)
        return [*out, action], kept if entry is None else [*kept, entry]

    return _without_deferred_clears(actions, reduce(fold, actions, ([], []))[0])


def claimed_by(actions: list[Action], facts: Facts) -> list[RunningInitiative]:
    """The entry of each initiative with a launch in plan_recover's `actions`, in order, one per initiative.

    It claims before the launch cap and placement run, so a launch they later drop still holds its claim this tick.
    """
    by_id = {i["id"]: i for i in facts["initiatives"]}
    entries = (
        e for a in actions if a["kind"] in _LAUNCHES and (i := by_id.get(a["initiative"])) is not None and (e := _entry(i))
    )
    return list({e["id"]: e for e in entries}.values())


def recover_blocked(facts: Facts) -> set[str]:
    """Initiatives whose open quarantines or schema deaths keep plan_recover from relaunching them."""
    approved = {(a["initiative"], a["id"]) for a in facts["approved"]}
    outcomes = facts.get("land_outcomes", {})
    quarantines = facts["quarantines"]
    hold_after = facts.get("quarantine_hold_after", DEFAULT_QUARANTINE_HOLD_AFTER)
    return {q["initiative"] for q in quarantines if _blocks(q, _recovery(q, approved, outcomes, hold_after))} | set(
        facts.get("schema_deaths", {})
    )


_RETRIES = {"retry", "rescue", "rebuild_quarantined"}


def _over_budget(facts: Facts, now: datetime | None, initiatives: Collection[str]) -> frozenset[str]:
    """The initiatives the launch budget refuses; kind "retry" is budget-only, the loop rule is for relaunches."""
    if now is None:
        return frozenset()
    return frozenset(i for i in initiatives if not budget_verdict(facts, i, "retry", now).allowed)


def _retry_initiatives(actions: list[Action]) -> set[str]:
    return {a["initiative"] for a in actions if a["kind"] in _RETRIES}


def _quarantine_plan(facts: Facts) -> list[Action]:
    approved = {(a["initiative"], a["id"]) for a in facts["approved"]}
    return _quarantine_actions(
        facts["quarantines"], approved, facts.get("land_outcomes", {}), facts.get("handled_keys", []),
        facts.get("quarantine_hold_after", DEFAULT_QUARANTINE_HOLD_AFTER),
    )


HELD_CAUSE = "held"


def _held_actions(held: list[HeldTask], handled: Collection[str]) -> list[Action]:
    """Per held task, one needs_chair each tick, so the status line lists it under `needs chair`, and one announce_hold
    until its key is handled, so the inbox gets one entry per hold reason."""
    actions: list[Action] = []
    for h in held:
        base: Action = {"initiative": h["initiative"], "task_id": h["task_id"], "reason": h["reason"]}
        actions.append({**base, "kind": "needs_chair", "cause": f"{HELD_CAUSE} {h['task_id']}"})
        if (key := chair_sort.announce_key(h["task_id"], h["reason"])) not in handled:
            actions.append({**base, "kind": "announce_hold", "key": key})
    return actions


def refused_launches(facts: Facts, now: datetime | None) -> frozenset[str]:
    """Initiatives plan_recover refuses to launch on the budget (relaunch, retry or rescue) or the loop rule.

    None when now is None."""
    relaunches = frozenset(refused_relaunches(facts, now, recover_blocked(facts)))
    return relaunches | _over_budget(facts, now, _retry_initiatives(_quarantine_plan(facts)))


def plan_recover(facts: Facts, now: datetime | None = None) -> list[Action]:
    """Schema-death reports, then quarantine actions in input order, then relaunch pairs, then waiting-on reports.

    A relaunch, retry or rescue that shares a surface with a running or already-kept initiative in the same repo
    is replaced by the steer_check action: a steer_clear, or needs_chair once the pair's streak reaches 3.

    An initiative in `schema_deaths` gets one schema-version needs_chair carrying the two run ids, and no
    relaunch, retry or rescue. Its quarantines still report their own needs_chair.

    An initiative a land in `landing` touches (landing_initiatives) gets no relaunch pair, so the pair never takes a
    launch slot; another initiative in the same repo relaunches as usual.

    An initiative with a `dag_problem` is never relaunched. It reaches the chair once, as `invalid_dag (<problem>)`.

    Every open quarantine blocks its initiative's relaunch except one whose recovery is "none", "relaunch", "triage" or "hold".
    A main_red quarantine recovers by relaunch until `land_outcomes` for its initiative ends in two reverts.
    Then its recovery is "none" and it keeps blocking.
    A failure that needs a tweak (code, review, ticket, unknown) plans one request_triage per task state (`triage:<task>:<run>`):
    the next run triages the task before any rebuild. Once the task's own failures reach `quarantine_hold_after` (default 3,
    0 never), or a requested triage left it quarantined in the same state, it plans one hold_task instead, which marks it
    `needs you:` for a person. A fact with no `run` names no state and reaches the chair.
    Each held task in `held_tasks` reports one needs_chair every tick and one announce_hold per hold reason.
    A runaway-ceiling quarantine always reaches the chair, ahead of every other rule: it is never retried or
    rescued, whatever its harness_failures, retry count or approval state.
    A one-failure harness quarantine is rescued if it kept a patch, retried if not, and goes to the chair once a
    rescue failed. A rescue goes out once per task state (`rescue:<task>:<run>`); the same state quarantined again
    after it reaches the chair. A stranded quarantine whose task is also an approved row of its initiative plans no action:
    `plan_lands` lands that task once its whole phase is done, approved or dropped, and recovery never lands it alone.
    A started initiative with a ready task but no ready task whose needs are all landed is not relaunched
    either; it reaches the chair instead, as `waiting on <need>`, naming the first unmet need.

    With `now`, a retry or rescue the launch budget refuses is dropped too. A relaunch the budget refuses plans no pair. A relaunch_loop refusal plans no pair and
    one needs_chair, unless facts["relaunch_loop_reported"] already holds its `<run1>|<run2>` key. Without `now`
    the budget is skipped.
    """
    quarantines = facts["quarantines"]
    approved = {(a["initiative"], a["id"]) for a in facts["approved"]}
    deaths = facts.get("schema_deaths", {})
    outcomes = facts.get("land_outcomes", {})
    blocked = recover_blocked(facts)
    refused = refused_relaunches(facts, now, blocked)
    handled = facts.get("handled_keys", [])
    hold_after = facts.get("quarantine_hold_after", DEFAULT_QUARANTINE_HOLD_AFTER)
    quarantine_actions = _quarantine_actions(quarantines, approved, outcomes, handled, hold_after)
    over = _over_budget(facts, now, _retry_initiatives(quarantine_actions))
    return _steered(
        _schema_death_actions(deaths)
        + _without_schema_dead_launches(_without_over_budget(quarantine_actions, over), deaths)
        + _held_actions(facts.get("held_tasks", []), handled)
        + _relaunch_actions(
            facts["initiatives"], blocked, landing_initiatives(facts), refused, facts.get("relaunch_loop_reported", [])
        )
        + _invalid_dag_actions(facts["initiatives"])
        + _waiting_actions(facts["initiatives"], blocked),
        facts,
    )


def plan_lost_runs(facts: Facts, now: datetime | None = None) -> list[Action]:
    """One mark_lost then the same clear_branches, relaunch pair, per initiative that is a key of lost_runs this tick.

    With `now`, an initiative the launch budget refuses keeps its mark_lost but gets no pair; the loop rule
    does not apply, as a lost run is not a quarantine.

    Does not check readiness, needs or run_exited: a host unreachable for ten minutes with no exit record is
    itself the evidence the previous process is gone.
    An initiative in `schema_deaths` keeps its mark_lost but gets no relaunch pair; `plan_recover` reports it.
    """
    deaths = facts.get("schema_deaths", {})
    over = _over_budget(facts, now, facts.get("lost_runs", {}))
    return [
        action
        for initiative, run in facts.get("lost_runs", {}).items()
        for action in (
            {"kind": "mark_lost", "initiative": initiative, "run": run},
            *([] if initiative in deaths or initiative in over else relaunch_pair(initiative)),
        )
    ]
