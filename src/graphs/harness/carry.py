"""Pure decision for an approved task whose merged commit is off the phase branch. No I/O, git, clock or store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

REASON_NOT_FOUND = "commit not found"
REASON_DOES_NOT_APPLY = "does not apply cleanly to the current base"

Probe = Literal["on_phase_branch", "claimed", "landed_on_base", "fetch", "apply"]


@dataclass(frozen=True)
class OnBase:
    """The change is already on the base, as this pull request."""

    pr_number: int


@dataclass(frozen=True)
class NotOnBase:
    pass


@dataclass(frozen=True)
class Fetched:
    """The commit was fetched from `source`, which must name where."""

    source: str

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("a fetched commit must name where it was fetched from")


@dataclass(frozen=True)
class NotFetched:
    pass


@dataclass(frozen=True)
class Facts:
    """What the git edge has learned so far. None means the probe has not run."""

    task_id: str
    merged_commit: str
    on_phase_branch: bool | None = None
    claimed_by_active_land: bool | None = None
    base: OnBase | NotOnBase | None = None
    fetch: Fetched | NotFetched | None = None
    applies_cleanly: bool | None = None


@dataclass(frozen=True)
class CarriedFrom:
    commit: str
    fetched_from: str


@dataclass(frozen=True)
class Skip:
    """Leave the task untouched."""


@dataclass(frozen=True)
class MarkDone:
    pr_number: int


@dataclass(frozen=True)
class Carry:
    carried_from: CarriedFrom


@dataclass(frozen=True)
class Rebuild:
    """The task goes back to ready, for the stated reason."""

    reason: str


Outcome = Skip | MarkDone | Carry | Rebuild


def next_step(facts: Facts) -> Probe | None:
    """The next probe the edge must run, in precedence order. None once `decide` can answer."""
    if facts.on_phase_branch is None:
        return "on_phase_branch"
    if facts.on_phase_branch:
        return None
    if facts.claimed_by_active_land is None:
        return "claimed"
    if facts.claimed_by_active_land:
        return None
    if facts.base is None:
        return "landed_on_base"
    if isinstance(facts.base, OnBase):
        return None
    if facts.fetch is None:
        return "fetch"
    if isinstance(facts.fetch, NotFetched):
        return None
    if facts.applies_cleanly is None:
        return "apply"
    return None


def decide(facts: Facts) -> Outcome | None:
    """The outcome, or None while `next_step` still names a probe. A claim or the phase branch beats a landed change."""
    if next_step(facts) is not None:
        return None
    if facts.on_phase_branch or facts.claimed_by_active_land:
        return Skip()
    if isinstance(facts.base, OnBase):
        return MarkDone(facts.base.pr_number)
    if isinstance(facts.fetch, NotFetched):
        return Rebuild(REASON_NOT_FOUND)
    if isinstance(facts.fetch, Fetched) and facts.applies_cleanly:
        return Carry(CarriedFrom(facts.merged_commit, facts.fetch.source))
    if isinstance(facts.fetch, Fetched) and facts.applies_cleanly is False:
        return Rebuild(REASON_DOES_NOT_APPLY)
    return None


def carried_from_record(outcome: Carry) -> dict[str, str]:
    """The dict stored on the task."""
    return {"commit": outcome.carried_from.commit, "fetched_from": outcome.carried_from.fetched_from}
