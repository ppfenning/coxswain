from collections.abc import Sequence

_SETTLED = frozenset({"done", "dropped", "approved"})


def phase_status(task_states: Sequence[str], validated: bool) -> str:
    """`done` when the phase has tasks, all in done, dropped or approved, and `validated`; else `open`."""
    return "done" if task_states and validated and all(s in _SETTLED for s in task_states) else "open"
