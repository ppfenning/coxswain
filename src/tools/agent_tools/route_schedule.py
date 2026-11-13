"""`route schedule`: a Schedule as lines of text. The core is pure; `plan_from_inputs` is the only builder here."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from agent_tools import chair_schedule_advice as advice_mod
from agent_tools.chair_schedule import Host, Running, Schedule, plan_schedule
from agent_tools.chair_schedule_durations import Estimate, estimate_duration
from agent_tools.chair_schedule_graph import Initiative, Key, Node, Phase, build_graph
from agent_tools.chair_steer import shared_paths
from agent_tools.chair_types import ScheduleInputs

# No phase record exists for a new shape; the planner owns no default, so a phase with no history takes an hour.
DEFAULT_SECONDS = 3600.0
NO_ASKER = "no asker ran"


@dataclass(frozen=True)
class Context:
    """What the Schedule does not carry and the explain output needs: the graph and each node's duration estimate."""

    nodes: Mapping[Key, Node]
    dependents: Mapping[Key, frozenset[Key]]
    durations: Mapping[Key, Estimate]


@dataclass(frozen=True)
class _Release:
    target: str
    projected_finish: str
    misses_target: bool


@dataclass(frozen=True)
class AdviceView:
    """A Schedule seen through the structural protocol `chair_schedule_advice` reads."""

    launches: Sequence
    ties: Sequence[Sequence[Key]]
    critical_path: Sequence[Key]
    critical_values: Mapping[Key, int]
    priorities: Mapping[Key, int]
    release: _Release | None


def _name(key: Key) -> str:
    return "/".join(key)


def _when(moment: datetime | None) -> str:
    return "unknown" if moment is None else moment.strftime("%Y-%m-%d %H:%M")


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


def plan_from_inputs(inputs: ScheduleInputs, now: datetime) -> tuple[Schedule, Context]:
    """Plan the queue from the facts' inputs. `now` is passed in; nothing here reads a clock."""
    mix = {(i["id"], p["id"]): p["mix"] for i in inputs["initiatives"] for p in i["phases"]}
    initiatives = [
        Initiative(
            i["id"],
            i["priority"],
            i["repo"],
            tuple(
                Phase(p["id"], p["status"], tuple(p["parents"]), p["size"], tuple(p["surfaces"])) for p in i["phases"]
            ),
        )
        for i in inputs["initiatives"]
    ]
    nodes, dependents = build_graph(initiatives)
    durations = {k: estimate_duration(inputs["records"], n.size, mix[k], DEFAULT_SECONDS) for k, n in nodes.items()}
    hosts = [Host(h["name"], tuple(h["lanes"]), h["local"], tuple(h["repos"])) for h in inputs["hosts"]]
    target = datetime.fromisoformat(inputs["target"]) if inputs["target"] else None
    # No finish is carried, so `now` marks each as overdue and it holds its lane for the planner's minimum.
    held = {
        (r["initiative"], r["phase"]): Running(r["host"], r["lane"], now)
        for r in inputs.get("running", [])
        if (r["initiative"], r["phase"]) in nodes
    }
    schedule = plan_schedule(nodes, dependents, durations, hosts, inputs["mode"], inputs["load"], now, held, target)
    return schedule, Context(nodes, dependents, durations)


def advice_view(schedule: Schedule, context: Context) -> AdviceView:
    release = (
        None
        if schedule.release_finish is None or schedule.target is None
        else _Release(schedule.target.isoformat(), schedule.release_finish.isoformat(), bool(schedule.misses_target))
    )
    return AdviceView(
        launches=schedule.launches,
        ties=tuple(t.keys for t in schedule.ties),
        critical_path=schedule.critical_path,
        critical_values={e.key: int(e.value) for e in schedule.explanations},
        priorities={k: n.priority for k, n in context.nodes.items()},
        release=release,
    )


def format_plain(schedule: Schedule, advice: Sequence[advice_mod.Advice] = ()) -> list[str]:
    """Lanes, initiatives, release, critical path and advice, in that order."""
    if schedule.cycle:
        return [f"cycle in the work graph: {' -> '.join(map(_name, schedule.cycle))}"]
    lanes = [
        f"{lane.host}/{lane.lane}: "
        + (
            "idle"
            if not lane.next
            else "; ".join(f"{_name(p.key)} {_when(p.start)} to {_when(p.finish)}" for p in lane.next)
        )
        for lane in schedule.lanes
    ]
    initiatives = [
        f"{i.initiative}: finishes {_when(i.projected_finish)} (priority {i.priority})"
        + (" on the critical path" if i.on_critical_path else "")
        for i in schedule.initiatives
    ]
    miss = {True: " MISSES TARGET", False: " meets target", None: ""}[schedule.misses_target]
    release = f"release: {_when(schedule.release_finish)}, target {_when(schedule.target)}{miss}"
    path = "critical path: " + (" -> ".join(map(_name, schedule.critical_path)) or "none")
    unplaced = [f"unplaced: {', '.join(map(_name, schedule.unplaced))}"] if schedule.unplaced else []
    notes = [f"advice ({a['kind']}, {a['at']}): {a['text']}" for a in advice]
    return ["lanes:", *lanes, "initiatives:", *initiatives, release, path, *unplaced, *notes]


def _waits(context: Context, key: Key) -> list[str]:
    """One line per phase of another initiative that must finish first, with the file that put it ahead."""
    node = context.nodes[key]
    return [
        f"  waits behind {_name(other)} on {', '.join(sorted(shared_paths(context.nodes[other].surfaces, node.surfaces)))}"
        for other, after in context.dependents.items()
        if key in after and other[0] != key[0]
    ]


def explain_lines(schedule: Schedule, context: Context, initiative: str) -> list[str] | None:
    """Why the initiative sits where it does; None when no open phase of it is in the schedule."""
    keys = [k for k in context.nodes if k[0] == initiative]
    if not keys:
        return None
    why = {e.key: e for e in schedule.explanations}
    phases = [
        line
        for key in keys
        for line in (
            f"{_name(key)}: critical path value {_hours(why[key].value) if key in why else 'unknown'}, "
            f"{_placed(why.get(key))}",
            f"  duration {_hours(context.durations[key].seconds)} from tier {context.durations[key].tier}"
            f" ({context.durations[key].n} records)"
            if key in context.durations
            else "  duration unknown",
            *_waits(context, key),
        )
    ]
    ties = [f"tie: {', '.join(map(_name, t.keys))}" for t in schedule.ties if any(k[0] == initiative for k in t.keys)]
    return [f"{initiative}: priority {context.nodes[keys[0]].priority}", *phases, *ties]


def _placed(why: object) -> str:
    return (
        "not placed"
        if why is None or getattr(why, "order", None) is None
        else f"pick {why.order} on {why.host}/{why.lane}, bound by {why.binding}"  # type: ignore[attr-defined]
    )


def should_jump_lines(
    schedule: Schedule,
    context: Context,
    initiative: str,
    asker: Callable[[str], str] | None,
    at: str,
) -> list[str] | None:
    """The advice a reorder question yields, or its prompt and `no asker ran`; None for an unknown initiative."""
    keys = tuple(k for k in context.nodes if k[0] == initiative)
    if not keys:
        return None
    view = advice_view(schedule, context)
    question = advice_mod.Question(f"Should {initiative} jump the queue?", keys)
    request = next(r for r in advice_mod.requests_for(view, question) if r.kind == "reorder")
    # `advise` asks once per request; only the reorder prompt reaches the model, tie and miss prompts get blank.
    only_reorder = None if asker is None else (lambda prompt: asker(prompt) if prompt == request.prompt else "")
    answers = advice_mod.advise(view, only_reorder, at, question)
    return (
        [request.prompt, NO_ASKER]
        if asker is None
        else [a["text"] for a in answers if a["kind"] == "reorder"] or ["the asker gave no advice"]
    )
