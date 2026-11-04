"""The per-run task list of the dash feed: pure, plain data in and plain data out."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

REASON_MAX = 120
DONE_STATES = frozenset({"done", "merged", "landed"})
SETTLED_STATES = frozenset({"done", "dropped"})


def cut_reason(text: str) -> tuple[str, str | None]:
    """The text cut to `REASON_MAX` characters, and the whole text only when it was cut."""
    return (text[:REASON_MAX], text) if len(text) > REASON_MAX else (text, None)


def waiting_on(needs: Sequence[str], state_by_id: Mapping[str, str]) -> list[str]:
    """The needs that are not yet done or dropped, in order; a need the store does not list is still waited on."""
    return [need for need in needs if state_by_id.get(need) not in SETTLED_STATES]


def _entry(
    row: Mapping, state_by_id: Mapping[str, str], attempts: Sequence, latest: Mapping, verdict: str | None,
) -> dict:
    task_id, state = row["task_id"], row.get("state") or ""
    reason, reason_full = cut_reason(str(row.get("reason") or ""))
    pending = waiting_on(row.get("needs") or (), state_by_id) if state == "ready" and not attempts else []
    return {
        "id": task_id,
        "phase": row.get("phase") or "",
        "state": state,
        "run": latest.get("run") or "",
        "tries": len(attempts),
        "node": latest.get("node") or "",
        "turns": latest.get("turns") or 0,
        "cost": latest.get("cost") or 0.0,
        "reason": reason,
        **({"reason_full": reason_full} if reason_full is not None else {}),
        **({"run_verdict": verdict} if verdict and verdict != state else {}),
        # The contract (both repos' dash_feed_tasks_v1.json) and towpath's `Option<String>` carry one task id:
        # the first need still pending. A list here fails towpath's parse of the whole feed line.
        **({"waiting_on": pending[0]} if pending else {}),
    }


def task_entries(
    task_rows: Sequence[Mapping],
    attempts_by_task: Mapping[str, Sequence],
    latest_by_task: Mapping[str, Mapping],
    verdicts_by_task: Mapping[str, str],
) -> list[dict]:
    """One entry per task row of one initiative, phases in first-seen row order; `state` is always the store's."""
    state_by_id = {row["task_id"]: row.get("state") or "" for row in task_rows}
    phases = list(dict.fromkeys(row.get("phase") or "" for row in task_rows))
    return [
        _entry(
            row, state_by_id, attempts_by_task.get(row["task_id"]) or (), latest_by_task.get(row["task_id"]) or {},
            verdicts_by_task.get(row["task_id"]),
        )
        for phase in phases
        for row in task_rows
        if (row.get("phase") or "") == phase
    ]


def counts(entries: Sequence[Mapping]) -> dict:
    """`{landed, quarantined}` over the entries; landed is every done-like state."""
    return {
        "landed": sum(1 for entry in entries if entry["state"] in DONE_STATES),
        "quarantined": sum(1 for entry in entries if entry["state"] == "quarantined"),
    }
