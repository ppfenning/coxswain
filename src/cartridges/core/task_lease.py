"""The name of a per-task land lease: written by `agent_tools.land_lease`, read by `harness.land_claims`."""

from __future__ import annotations

TASK_LEASE_PREFIX = "land:task:"


def task_lease_name(initiative: str, task: str) -> str:
    """Pure. The lease name of a task land that says which initiative it belongs to; `parse_task_lease_name`
    reverses it."""
    return f"{TASK_LEASE_PREFIX}{initiative}/{task}"


def parse_task_lease_name(name: str) -> tuple[str, str] | None:
    """Pure. (initiative, task) for a `task_lease_name`; None for a bare `land:<task>`, a phase land, or a repo
    lease."""
    if not name.startswith(TASK_LEASE_PREFIX):
        return None
    initiative, slash, task = name.removeprefix(TASK_LEASE_PREFIX).partition("/")
    return (initiative, task) if slash and initiative and task else None
