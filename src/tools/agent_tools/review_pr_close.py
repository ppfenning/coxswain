"""Pure core: pick a landed task's review PRs and word the close comment."""

from collections.abc import Collection, Mapping, Sequence


def review_branch(initiative: str, task: str) -> str:
    return f"review/{initiative}/{task}"


def review_prs_for_task(
    initiative: str, task: str, prs: Sequence[Mapping]
) -> list[Mapping]:
    """PRs whose head is exactly the review branch; no prefix or suffix match."""
    branch = review_branch(initiative, task)
    return [pr for pr in prs if pr.get("head") == branch]


def close_comment(sha: str, task: str) -> str:
    return f"Landed {task} as {sha}."


def _mapped_tasks(head: object, task_rows: Sequence[Mapping]) -> list[Mapping]:
    """Tasks a `review/<initiative>/<subject>` head names; a task id wins over a phase of the same name."""
    parts = head.split("/") if isinstance(head, str) else []
    if len(parts) != 3 or parts[0] != "review" or not parts[1] or not parts[2]:
        return []
    _, initiative, subject = parts
    own = [row for row in task_rows if row.get("initiative") == initiative]
    by_id = [row for row in own if row.get("task_id") == subject]
    return by_id or [row for row in own if row.get("phase") == subject]


def closable_prs(
    prs: Sequence[Mapping], task_rows: Sequence[Mapping], closed_states: Collection[str]
) -> list[tuple[int, str, tuple[str, ...]]]:
    """(number, head, task_ids) per PR whose mapped tasks are all in closed_states, in input order."""
    mapped = [(pr, _mapped_tasks(pr.get("head"), task_rows)) for pr in prs]
    return [
        (pr["number"], pr["head"], tuple(row["task_id"] for row in rows))
        for pr, rows in mapped
        if isinstance(pr.get("number"), int)
        and not isinstance(pr["number"], bool)
        and rows
        and all(row.get("state") in closed_states for row in rows)
    ]


def swept_comment(task_ids: Sequence[str]) -> str:
    return f"Closed: {', '.join(task_ids)} done or removed with no single landing."
