"""Pure remedy choice and reason trim for a quarantined task. No I/O, no clock."""

from typing import TypedDict

_REASON_LIMIT = 600

_REMEDY_BY_CAUSE = {
    "ticket": "re-ground",
    "code": "re-ground",
    "review": "re-ground",
    "stranded": "carry",
    "harness": "retry",
    "stalled": "retry",
    "stale": "drop",
    "superseded": "drop",
}

# A drop or a retry moves the ticket with `cox route edit --state`, which writes the file and the store together through
# the store API. `cox inbox` runs these argvs as they are. They once ran `python -m harness.store_cli set-state` with the
# run in place of the initiative and no store named, so every accept and deny failed (H1).


def trim_reason(text: str | None) -> str:
    """Strip the text and cut it to 600 characters; None gives an empty string."""
    return (text or "").strip()[:_REASON_LIMIT]


def choose_remedy(cause: str | None) -> str:
    """Fixed rule: any cause not listed, and a missing cause, gives re-ground."""
    return _REMEDY_BY_CAUSE.get(cause or "", "re-ground")


def set_state_argv(initiative: str, task: str, state: str) -> list[str]:
    """The `route edit` argv that moves one ticket. It names the initiative, so a task id repeated across initiatives
    moves this initiative's ticket; an empty initiative (a row that carries none) gives the id-only argv, which
    `route edit` refuses rather than guess when the id repeats."""
    return ["cox", "route", "edit", task, *(["--initiative", initiative] if initiative else []), "--state", state]


def initiative_of(initiative: str, run: str) -> str:
    """The initiative a row names, else the one its run id names: `x-3` and the phase run `x.p1-2` are both `x`."""
    if initiative:
        return initiative
    series, _, tail = run.rpartition("-")
    return series.split(".", 1)[0] if series and tail.isdigit() else ""


def remedy_command(kind: str, run: str, task: str, repo: str, initiative: str = "") -> list[str]:
    """Argv of the existing command for a remedy kind; an unknown kind gets the re-ground argv."""
    if kind == "carry":
        return ["cox", "runs", "land", run, "--repo", repo, "--task", task, "--apply"]
    if kind == "drop":
        # No dedicated drop verb exists: the nearest set-state argv marks the ticket dropped.
        return set_state_argv(initiative, task, "dropped")
    # Retry has no dedicated verb either: the nearest set-state argv puts the ticket ready. Re-ground and unknown kinds too.
    return set_state_argv(initiative, task, "ready")


class Remedy(TypedDict):
    kind: str
    command: list[str]


def remedy_for(cause: str | None, run: str, task: str, repo: str, initiative: str = "") -> Remedy:
    kind = choose_remedy(cause)
    return Remedy(kind=kind, command=remedy_command(kind, run, task, repo, initiative))
