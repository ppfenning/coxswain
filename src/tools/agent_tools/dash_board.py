"""The board tree of the dash feed: pure, plain data in and plain data out."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

STATUSES = ("ready", "building", "approved", "merged", "landed", "quarantined", "dropped", "blocked")
# A parent shows the first status in this list that any live child has.
ROLLUP_ORDER = ("building", "quarantined", "blocked", "approved", "merged", "ready")
NO_EPIC_TITLE = "no epic"
# An initiative with one of these rolled-up statuses is settled: the board counts it and does not list it.
SETTLED = ("landed", "dropped")


def task_costs(
    attempts_by_task: Mapping[str, Sequence[Mapping]], calls_by_run: Mapping[str, Sequence[Mapping]],
) -> dict[str, float]:
    """Each task's cost over the calls stamped with its id in every run it was attempted in, to 4 places."""
    def runs_of(attempts: Sequence[Mapping]) -> list[str]:
        return list(dict.fromkeys(str(a["run_id"]) for a in attempts if a.get("run_id")))

    return {
        task: round(
            sum(
                float(call.get("cost_usd") or 0.0)
                for run in runs_of(attempts)
                for call in calls_by_run.get(run) or ()
                if call.get("task_id") == task
            ),
            4,
        )
        for task, attempts in attempts_by_task.items()
    }


def rollup_status(statuses: Sequence[str]) -> str:
    """A parent's status from its children's: dropped children are ignored, and none left means dropped."""
    live = [status for status in statuses if status != "dropped"]
    return next((status for status in ROLLUP_ORDER if status in live), "landed") if live else "dropped"


def _counts(statuses: Sequence[str]) -> dict[str, int]:
    return {status: sum(1 for s in statuses if s == status) for status in STATUSES}


def _sum_counts(children: Sequence[Mapping]) -> dict[str, int]:
    return {status: sum(child["counts"][status] for child in children) for status in STATUSES}


def _parent(head: dict, key: str, children: list[dict]) -> dict:
    return {
        **head,
        "status": rollup_status([child["status"] for child in children]),
        "cost_usd": round(sum(child["cost_usd"] for child in children), 4),
        "counts": _sum_counts(children),
        key: children,
    }


def _phase(phase: str, tasks: list[dict]) -> dict:
    return {
        "id": phase,
        "status": rollup_status([task["status"] for task in tasks]),
        "cost_usd": round(sum(task["cost_usd"] for task in tasks), 4),
        "counts": _counts([task["status"] for task in tasks]),
        "tasks": tasks,
    }


def _settle(epic: dict) -> dict:
    """The epic with only its open initiatives listed and the settled ones reduced to counts and cost."""
    settled = [i for i in epic["initiatives"] if i["status"] in SETTLED]
    return {
        **epic,
        "initiatives": [i for i in epic["initiatives"] if i["status"] not in SETTLED],
        "settled": {
            "landed": sum(1 for i in settled if i["status"] == "landed"),
            "dropped": sum(1 for i in settled if i["status"] == "dropped"),
            "cost_usd": round(sum(i["cost_usd"] for i in settled), 4),
        },
    }


def build_board(
    task_rows: Sequence[Mapping],
    states: Mapping[tuple[str, str, str], str],
    runs: Mapping[str, tuple[str, str]],
    epic_of: Mapping[str, str | None],
    short_ids: Mapping[str, str],
    costs: Mapping[str, float],
) -> dict:
    """The epics, initiatives, phases and tasks of the task rows, each level with status, cost and counts.
    A row with no entry in `states` is left out; an initiative with no epic goes under id "" titled "no epic".
    An epic's status, counts and cost cover every initiative; it lists only the open ones, and `settled` holds the rest."""
    keys = [
        (row["initiative"], row.get("phase") or "", row["task_id"])
        for row in task_rows
        if (row["initiative"], row.get("phase") or "", row["task_id"]) in states
    ]

    def task(key: tuple[str, str, str]) -> dict:
        run, host = runs.get(key[2], ("", ""))
        return {"id": key[2], "status": states[key], "run": run, "host": host, "cost_usd": costs.get(key[2], 0.0)}

    def initiative(slug: str) -> dict:
        own = [key for key in keys if key[0] == slug]
        phases = [_phase(phase, [task(key) for key in own if key[1] == phase]) for phase in sorted({k[1] for k in own})]
        return _parent({"id": slug, "short_id": short_ids.get(slug) or ""}, "phases", phases)

    def epic(epic_id: str) -> dict:
        slugs = sorted({key[0] for key in keys if (epic_of.get(key[0]) or "") == epic_id})
        head = {"id": epic_id, "title": epic_id or NO_EPIC_TITLE}
        return _settle(_parent(head, "initiatives", [initiative(slug) for slug in slugs]))

    epic_ids = sorted({epic_of.get(key[0]) or "" for key in keys}, key=lambda epic_id: (epic_id == "", epic_id))
    return {"epics": [epic(epic_id) for epic_id in epic_ids]}
