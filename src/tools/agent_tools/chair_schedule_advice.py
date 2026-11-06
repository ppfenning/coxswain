"""When the scheduler needs a model, and a record of what it said.

The pure part turns a Schedule into requests for the dispatch-graphs skill. The edge part maps each
request's prompt through an injected asker and returns advice entries in the feed form's shape
(`kind`, `text`, `at`). Advice sits beside a Schedule and never alters its launches.

Nodes are `chair_schedule_graph.Key` tuples, `(initiative, phase)`, shown as `initiative/phase`.
`Schedule` below is the structural view this module reads. Its `release` follows the feed form's
`release` object and may be None. The sched-pack `chair_schedule.Schedule` was not in the checkout
this was written against, so the other field names are unverified against it.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from agent_tools.chair_schedule_graph import Key

Kind = Literal["tie", "miss", "reorder"]
Asker = Callable[[str], str]


class Release(Protocol):
    @property
    def target(self) -> str: ...
    @property
    def projected_finish(self) -> str: ...
    @property
    def misses_target(self) -> bool: ...


class Schedule(Protocol):
    @property
    def launches(self) -> Sequence: ...
    @property
    def ties(self) -> Sequence[Sequence[Key]]: ...  # groups of nodes the schedule could not order
    @property
    def critical_path(self) -> Sequence[Key]: ...  # nodes in order, the chain behind the release
    @property
    def critical_values(self) -> Mapping[Key, int]: ...  # node to work stuck behind it
    @property
    def priorities(self) -> Mapping[Key, int]: ...  # node to priority, higher runs first
    @property
    def release(self) -> Release | None: ...


class Advice(TypedDict):
    kind: Kind
    text: str
    at: str


@dataclass(frozen=True)
class Question:
    """An operator question and the nodes it is about, so the prompt can name their inputs."""

    text: str
    nodes: tuple[Key, ...]


@dataclass(frozen=True)
class AdviceRequest:
    kind: Kind
    nodes: tuple[Key, ...]
    prompt: str


def _name(node: Key) -> str:
    return "/".join(node)


def _show(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _line(schedule: Schedule, node: Key) -> str:
    path, priority = schedule.critical_values.get(node), schedule.priorities.get(node)
    return f"- {_name(node)}: critical path {_show(path)}, priority {_show(priority)}"


def _dates(schedule: Schedule) -> tuple[str, str]:
    release = schedule.release
    return (
        ("Projected release finish: none", "Release target: none")
        if release is None
        else (f"Projected release finish: {release.projected_finish}", f"Release target: {release.target}")
    )


def _render(
    schedule: Schedule, kind: Kind, nodes: tuple[Key, ...], ask: str, notes: tuple[str, ...] = ()
) -> AdviceRequest:
    prompt = "\n".join(
        (
            f"Use the dispatch-graphs skill. Judgment call: {kind}.",
            ask,
            *notes,
            "Contested nodes:",
            *((_line(schedule, node) for node in nodes) if nodes else ("- none named",)),
            *_dates(schedule),
        )
    )
    return AdviceRequest(kind=kind, nodes=nodes, prompt=prompt)


def requests_for(schedule: Schedule, question: Question | None = None) -> tuple[AdviceRequest, ...]:
    """One tie request for all unbroken ties, one miss on a missed target, one reorder per question."""
    tied = tuple(dict.fromkeys(node for group in schedule.ties for node in group))
    groups = tuple(f"Tie group {i}: {', '.join(map(_name, group))}" for i, group in enumerate(schedule.ties, 1))
    ties = (
        (_render(schedule, "tie", tied, "These nodes tie after every rule. Say which should launch first.", groups),)
        if tied
        else ()
    )
    path = tuple(schedule.critical_path)
    misses = (
        (
            _render(
                schedule, "miss", path, "The release projection misses its target. Say what could move to recover it."
            ),
        )
        if schedule.release is not None and schedule.release.misses_target
        else ()
    )
    reorders = (
        (_render(schedule, "reorder", question.nodes, f"Operator question: {question.text}"),)
        if question is not None
        else ()
    )
    return ties + misses + reorders


def _ask(asker: Asker, request: AdviceRequest, at: str) -> Advice | None:
    """None when the asker raises or says nothing: a model outage must not stop scheduling, and blank text is no advice."""
    try:
        text = asker(request.prompt)
    except Exception:
        return None
    return Advice(kind=request.kind, text=text, at=at) if text.strip() else None


def advise(schedule: Schedule, asker: Asker | None, at: str, question: Question | None = None) -> tuple[Advice, ...]:
    """Advice entries for the schedule, empty with no asker. The asker is called once per request."""
    return (
        ()
        if asker is None
        else tuple(a for r in requests_for(schedule, question) if (a := _ask(asker, r, at)) is not None)
    )
