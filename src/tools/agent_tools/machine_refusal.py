"""A worker's launch refused by the machine, not by the phase: a dirty or missing checkout, a harness venv that is not
there, a lane interpreter or a repo's tool that this machine lacks, no lane free here.

`cox route launch` runs a guard for each before any work (`cli_route_launch`); for a worker's launch (`--lane-worker`)
a refusal of this machine's also prints one marked line (`marked`), which the worker reads back (`of`). Such a refusal
says nothing about the phase, so it costs the phase no attempt, and another machine may build it at once: the worker
gives the claim back with no wait (`verdict`) and stops advertising the repository's `repo:<name>` until the cause has
gone (`cleared`): a checkout once `git status` reads it clean, anything else after `RETRY_S`. The chair is told once,
when the repository is first set aside (`set_aside`), not on every claim it would have refused.

Pure: the clock, the checkout's state and the chair's courier are the worker's ports (`worker_ports`).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from harness import phase_pick

__all__ = ["KINDS", "MARK", "RETRY_S", "Refusal", "SetAside", "advertised", "chair_note", "cleared", "marked", "of",
           "repo_name", "set_aside", "verdict"]

MARK = "routing: refused by this machine, not the phase"
Kind = Literal["checkout", "machine"]
KINDS: tuple[Kind, ...] = ("checkout", "machine")
RETRY_S = 600.0  # a refusal that no checkout read can clear is tried again after this, as the lane checks are
_REASON_CHARS = 300


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


def verdict(refusal: Refusal, attempts: int) -> phase_pick.Verdict:
    """The claim goes back at once with its attempt refunded: the phase is untouched, and another machine may take it."""
    return replace(phase_pick.refusal(f"this machine cannot launch it ({refusal.kind}): {refusal.reason}", attempts),
                   wait_s=0)


def repo_name(repo: str) -> str:
    """The `<name>` of a repository's `repo:<name>` token (`worker.repo_tokens`)."""
    return repo.rstrip("/").rpartition("/")[2]


def set_aside(current: Mapping[str, SetAside], repo: str, refusal: Refusal, now: float) -> tuple[dict[str, SetAside], bool]:
    """The repositories set aside once `repo` is, and whether that is news (it was not set aside already), which is
    when the chair is told. A repository set aside again keeps its first time and takes the latest refusal."""
    before = current.get(repo)
    entry = SetAside(repo, refusal, before.since if before is not None else now)
    return {**current, repo: entry}, before is None


def cleared(entry: SetAside, checkout_clean: bool | None, now: float, retry_s: float = RETRY_S) -> bool:
    """Whether `entry`'s cause has gone: a checkout's once it reads clean (`checkout_clean`, None when it was not
    read), any other once `retry_s` has passed since it was set aside."""
    if entry.refusal.kind == "checkout":
        return checkout_clean is True
    return now - entry.since >= retry_s


def advertised(present: Collection[str], aside: Collection[str]) -> list[str]:
    """The repositories with a checkout here that the worker still advertises: those not set aside."""
    return sorted(r for r in present if r not in aside)


def chair_note(worker: str, initiative: str, phase: str, entry: SetAside) -> str:
    """What the chair is told, once, when `entry`'s repository is set aside on `worker`."""
    until = "its checkout reads clean" if entry.refusal.kind == "checkout" else f"{int(RETRY_S)}s have passed"
    return (f"needs_attention: worker {worker} cannot launch repo:{repo_name(entry.repo)} ({entry.refusal.kind}): "
            f"{entry.refusal.reason}; {initiative}/{phase} went back with no attempt counted, and {worker} stops "
            f"advertising repo:{repo_name(entry.repo)} until {until}")
