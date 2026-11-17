"""Pure remedy choice and reason trim for a quarantined task. No I/O, no clock."""

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


def _set_state(task: str, state: str) -> list[str]:
    return ["cox", "route", "edit", task, "--state", state]


def remedy_command(kind: str, run: str, task: str, repo: str) -> list[str]:
    """Argv of the existing command for a remedy kind; an unknown kind gets the re-ground argv."""
    if kind == "carry":
        return ["cox", "runs", "land", run, "--repo", repo, "--task", task, "--apply"]
    if kind == "drop":
        # No dedicated drop verb exists: the nearest set-state argv marks the ticket dropped.
        return _set_state(task, "dropped")
    # Retry has no dedicated verb either: the nearest set-state argv puts the ticket ready. Re-ground and unknown kinds too.
    return _set_state(task, "ready")


def remedy_for(cause: str | None, run: str, task: str, repo: str) -> dict:
    kind = choose_remedy(cause)
    return {"kind": kind, "command": remedy_command(kind, run, task, repo)}
