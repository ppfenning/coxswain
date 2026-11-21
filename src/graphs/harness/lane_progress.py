"""What a phase branch already holds, and what a lane claiming it does next, pure (plan §1.4).

Git is the authority for "built and checked": each task rolls up as one commit on the phase branch, carrying `Cox-*`
trailers (`lane_rollup.message`). A lane that claims a phase reads the branch's log since its base (`LOG_ARGS`), and
`read` turns it into the tasks already on it and the revision rounds present. A commit whose initiative or phase
trailer names another phase, or that carries no `Cox-Task`, is not progress: it is listed in `ignored`, so the lane can
say so. `resume_plan` then joins that with the store's last review round of the phase to give the one next action.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = ["LOG_ARGS", "Next", "Progress", "read", "resume_plan"]

_TRAILERS = ("Cox-Initiative", "Cox-Phase", "Cox-Task", "Cox-Kind", "Cox-Round")
_FIELD, _RECORD = "\x1f", "\x1e"
# `git log` arguments, before the range: oldest first, one record per commit, the sha then each trailer's value.
LOG_ARGS: tuple[str, ...] = (
    "--reverse",
    "--format=%H" + "".join(f"%x1f%(trailers:key={k},valueonly)" for k in _TRAILERS) + "%x1e",
)


@dataclass(frozen=True)
class Progress:
    """The phase branch as progress: each task's commit (`done`, task id to sha, the last commit for a task wins),
    the review rounds a revision commit exists for, and the shas of commits that are not this phase's progress."""

    done: dict[str, str]
    revisions: tuple[int, ...]
    ignored: tuple[str, ...]


def _value(text: str) -> str:
    """A trailer's value as `%(trailers:valueonly)` prints it: the first line, stripped."""
    return (text.strip().splitlines() or [""])[0].strip()


def read(log_text: str, initiative: str, phase: str) -> Progress:
    """`log_text` (the output of `git log` with `LOG_ARGS`) as the phase's progress."""
    done: dict[str, str] = {}
    revisions: set[int] = set()
    ignored: list[str] = []
    for record in log_text.split(_RECORD):
        if not record.strip():
            continue
        sha, *values = [*record.strip("\n").split(_FIELD), *[""] * len(_TRAILERS)][: len(_TRAILERS) + 1]
        sha = sha.strip()
        got_initiative, got_phase, task, kind, round_text = (_value(v) for v in values)
        if got_initiative != initiative or got_phase != phase or not task:
            ignored.append(sha)
            continue
        if kind == "revision":
            if not round_text.isdigit():
                ignored.append(sha)
                continue
            revisions.add(int(round_text))
        done[task] = sha
    return Progress(done, tuple(sorted(revisions)), tuple(ignored))


Action = Literal["build", "review", "build_revision", "finish"]


@dataclass(frozen=True)
class Next:
    """The lane's next action. `build`: the tasks in `tasks`, in the order given; `review`: smoke at the head, then
    review round `round`; `build_revision`: the revision task of round `round`; `finish`: write `outcome`."""

    action: Action
    tasks: tuple[str, ...] = ()
    round: int = 0
    outcome: str = ""


def resume_plan(tasks: Sequence[str], done: Sequence[str], last_round: tuple[int, str] | None,
                revisions: Sequence[int] = ()) -> Next:
    """What a lane does on a claim, from the phase's tasks (in the order to build them), the tasks git shows rolled
    up, the store's last review round of the phase as (round, verdict), and the rounds git shows a revision for.

    A round that approved or quarantined finishes the phase with that outcome, whatever git shows. A revise round
    needs its revision task built, unless git already holds it, then the next round. With no round yet, the missing
    tasks build first; once none is missing the phase goes to review, round 1."""
    if last_round is not None:
        number, verdict = last_round
        if verdict == "approve":
            return Next("finish", outcome="approved")
        if verdict == "quarantine":
            return Next("finish", outcome="quarantined")
        if verdict == "revise":
            return Next("review", round=number + 1) if number in revisions else Next("build_revision", round=number)
    built = set(done)
    missing = tuple(t for t in tasks if t not in built)
    return Next("build", tasks=missing) if missing else Next("review", round=1)
