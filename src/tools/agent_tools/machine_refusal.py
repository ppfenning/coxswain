"""A worker's launch refused by the machine, not by the phase: a dirty or missing checkout, a harness venv that is not
there, a lane interpreter or a repo's tool that this machine lacks, no lane free here.

`cox route launch` runs a guard for each before any work (`cli_route_launch`); for a worker's launch (`--lane-worker`)
a refusal of this machine's also prints one marked line (`marked`), which the worker reads back (`of`). Such a refusal
says nothing about the phase, so it costs the phase no attempt, and another machine may build it at once: the worker
gives the claim back with no wait (`verdict`) and stops advertising the repository's `repo:<name>` until the cause has
gone (`cleared`): a checkout once `git status` reads it clean, anything else after `RETRY_S`.

The refund is bounded, as a lost lane's is (`phase_pick.LOST_REFUNDS`): a phase's episode gives back `REFUNDS`
refusals of a machine's, counted on the row's reason (`with_refunds`), and past them each counts its attempt like any
failed launch, so a phase that no machine can launch (a tool none has) still runs out of attempts and fails with the
reason rather than going round the fleet for ever. The chair is told once an episode for each reason (`tell`): on the
episode's first such refusal, and again only when the reason changes.

Pure: the clock, the checkout's state and the chair's courier are the worker's ports (`worker_ports`).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from harness import phase_pick

__all__ = ["KINDS", "MARK", "REFUNDS", "RETRY_S", "Refusal", "SetAside", "advertised", "carry_refunds", "chair_note",
           "cleared", "line", "marked", "of", "refunds_of", "repo_name", "set_aside", "tell", "verdict", "with_refunds"]

MARK = "routing: refused by this machine, not the phase"
Kind = Literal["checkout", "machine"]
KINDS: tuple[Kind, ...] = ("checkout", "machine")
RETRY_S = 600.0  # a refusal that no checkout read can clear is tried again after this, as the lane checks are
REFUNDS = 3  # refusals of a machine's an episode gives back; past them each counts its attempt
_REASON_CHARS = 300
_TAG = re.compile(r" \[machine refusals refunded: (\d+)\]")


@dataclass(frozen=True)
class Refusal:
    """Why this machine refused the launch: `checkout` when the repository's checkout is dirty or unreadable here,
    `machine` for anything else of this machine's."""

    kind: Kind
    reason: str


@dataclass(frozen=True)
class SetAside:
    """A repository this worker no longer advertises: the refusal that set it aside, and when (the worker's own
    monotonic clock)."""

    repo: str
    refusal: Refusal
    since: float


def marked(kind: Kind, reason: str) -> str:
    """The line a worker's launch prints after a refusal of this machine's: `MARK`, the kind, and the reason on one
    line."""
    return f"{MARK} ({kind}): {' '.join(reason.split())[:_REASON_CHARS]}"


def of(returncode: int, output: str) -> Refusal | None:
    """The machine's refusal a launch that exited `returncode` printed (its last marked line), None when it printed
    none. Only an exit 2, a refusal's, counts."""
    if returncode != 2:
        return None
    prefix = f"{MARK} ("
    for line in reversed(output.splitlines()):
        text = line.strip()
        if not text.startswith(prefix):
            continue
        head, sep, reason = text[len(prefix):].partition("): ")
        if sep and head in KINDS:
            return Refusal("checkout" if head == "checkout" else "machine", reason)
    return None


def line(refusal: Refusal) -> str:
    """How the refusal reads on the phase row's reason."""
    return f"this machine cannot launch it ({refusal.kind}): {refusal.reason}"


def refunds_of(reason: str) -> int:
    """How many refusals of a machine's the phase's episode has had refunded, from the tag on the row's reason; 0 when it
    carries none. A reopen writes the chair's own reason, so the next episode starts at 0."""
    found = _TAG.search(reason)
    return int(found.group(1)) if found else 0


def with_refunds(reason: str, refunds: int) -> str:
    """`reason` with its tag set to `refunds` (none at 0). The tag goes before a lost-lane tag, which must end the
    reason (`phase_pick.with_refunds`)."""
    lost = phase_pick.refunds_of(reason)
    bare = _TAG.sub("", phase_pick.with_refunds(reason, 0))
    return phase_pick.with_refunds(f"{bare} [machine refusals refunded: {refunds}]" if refunds > 0 else bare, *lost)


def carry_refunds(prior: str, reason: str) -> str:
    """A retry's `reason` carrying the episode's count from the claim's `prior` reason, unless it has its own: a retry
    for any other cause keeps it, so another kind of refund in between never starts the count again."""
    return reason if _TAG.search(reason) else with_refunds(reason, refunds_of(prior))


def verdict(refusal: Refusal, attempts: int, prior: str = "") -> phase_pick.Verdict:
    """The nack for a claim this machine refused, after the claim's `prior` reason. While the episode has refunds left
    (`REFUNDS`) the claim goes back at once with its attempt refunded: the phase is untouched, and another machine may
    take it. Past them the attempt counts, as a failed launch's does, so the phase fails with this reason in the end."""
    refunds = refunds_of(prior)
    if refunds < REFUNDS:
        found = phase_pick.refusal(with_refunds(line(refusal), refunds + 1), attempts)
        return replace(found, wait_s=0)
    why = f"{line(refusal)}; counted, after {REFUNDS} refunded this episode"
    found = phase_pick.verdict([], attempts, launch_error=why)
    return replace(found, reason=with_refunds(found.reason, refunds))


def tell(prior: str, refusal: Refusal) -> bool:
    """Whether the chair is told of this refusal: the episode's first refusal of a machine's, or one whose reason is not
    the one the row already says."""
    return refunds_of(prior) == 0 or line(refusal) not in prior


def repo_name(repo: str) -> str:
    """The `<name>` of a repository's `repo:<name>` token (`worker.repo_tokens`)."""
    return repo.rstrip("/").rpartition("/")[2]


def set_aside(current: Mapping[str, SetAside], repo: str, refusal: Refusal, now: float) -> dict[str, SetAside]:
    """The repositories set aside once `repo` is. A repository set aside again keeps its first time and takes the
    latest refusal."""
    before = current.get(repo)
    return {**current, repo: SetAside(repo, refusal, before.since if before is not None else now)}


def cleared(entry: SetAside, checkout_clean: bool | None, now: float, retry_s: float = RETRY_S) -> bool:
    """Whether `entry`'s cause has gone: a checkout's once it reads clean (`checkout_clean`, None when it was not
    read), any other once `retry_s` has passed since it was set aside."""
    if entry.refusal.kind == "checkout":
        return checkout_clean is True
    return now - entry.since >= retry_s


def advertised(present: Collection[str], aside: Collection[str]) -> list[str]:
    """The repositories with a checkout here that the worker still advertises: those not set aside."""
    return sorted(r for r in present if r not in aside)


def chair_note(worker: str, initiative: str, phase: str, entry: SetAside, counted: bool = False) -> str:
    """What the chair is told (`tell`) when `entry`'s repository is set aside on `worker`; `counted` when the episode's
    refunds were spent and this refusal counted its attempt."""
    until = "its checkout reads clean" if entry.refusal.kind == "checkout" else f"{int(RETRY_S)}s have passed"
    spent = "counted its attempt, the episode's refunds spent" if counted else "went back with no attempt counted"
    return (f"needs_attention: worker {worker} cannot launch repo:{repo_name(entry.repo)} ({entry.refusal.kind}): "
            f"{entry.refusal.reason}; {initiative}/{phase} {spent}, and {worker} stops "
            f"advertising repo:{repo_name(entry.repo)} until {until}")
