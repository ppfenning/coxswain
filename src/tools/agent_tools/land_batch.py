"""The step plan that lands one batch of approved phases as one pull request.

Pure: sources and facts in, a list of step dicts out. Nothing is executed or read here.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_tools.land import checks_step, pr_footer


def _source_id(source: Mapping[str, Any]) -> str:
    return f"{source['initiative']}/{source['phase']}"


def _branch(source: Mapping[str, Any]) -> str:
    return f"epic/{_source_id(source)}"


def _pr_title(sources: Sequence[Mapping[str, Any]]) -> str:
    return f"queue: {len(sources)} phases - {', '.join(_source_id(s) for s in sources)}"


def _source_block(source: Mapping[str, Any]) -> str:
    return "\n".join([_source_id(source),
                      f"  Tasks: {', '.join(source['tasks'])}",
                      f"  Files: {', '.join(source['files'])}"])


def _pr_body(sources: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join([*[_source_block(s) for s in sources], pr_footer(sources[0].get("run"))])


def _squash_steps(sources: Sequence[Mapping[str, Any]], batch_branch: str, default_branch: str) -> list[dict[str, Any]]:
    return [{"kind": "batch_squash", "branch": _branch(s), "onto": batch_branch, "from": default_branch,
             "subject": f"epic {s['initiative']}: {s['phase']}", "source": _source_id(s), "first": i == 0}
            for i, s in enumerate(sources)]


def _tail_steps(sources: Sequence[Mapping[str, Any]], batch_branch: str, task_paths: Mapping[str, str]) -> list[dict[str, Any]]:
    clean = [{"kind": "clean_phase", "run": s.get("run"), "phase_branch": _branch(s), "pr_branch": batch_branch,
              "tasks": list(s["tasks"])} for s in sources]
    tasks = list(dict.fromkeys(t for s in sources for t in s["tasks"]))
    done = [{"kind": "mark_done", "task": t, **({"path": task_paths[t]} if t in task_paths else {"recorded": False})}
            for t in tasks]
    return [*clean, *done]


def _refusal(sources: Sequence[Mapping[str, Any]], task_paths: Mapping[str, str]) -> str | None:
    ids = [_source_id(s) for s in sources]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    empty = [_source_id(s) for s in sources if not s["tasks"]]
    items = {t for s in sources for t in s.get("task_items", {})}
    unmarkable = list(dict.fromkeys(t for s in sources for t in s["tasks"] if t not in task_paths and t not in items))
    if repeated:
        return f"{', '.join(repeated)}: listed more than once in the batch; nothing merged"
    if empty:
        return f"{', '.join(empty)}: no tasks, so the PR would carry nothing identifiable; nothing merged"
    if unmarkable:
        return f"{', '.join(unmarkable)}: no record path and no approved work item to mark done; nothing merged"
    return None


def batch_plan(sources: Sequence[Mapping[str, Any]], repo_facts: dict[str, Any], default_branch: str,
               task_paths: Mapping[str, str], key: str) -> list[dict[str, Any]]:
    """Steps for one batch. `key` is the batch key; the batch branch is `queue/<key>`. The merge is a rebase, never a squash, so every source commit survives."""
    if not sources:
        return [{"kind": "note", "reason": "batch is empty: nothing to land"}]
    reason = _refusal(sources, task_paths)
    if reason is not None:
        return [{"kind": "refuse", "reason": reason}]
    batch_branch = f"queue/{key}"
    return [
        *_squash_steps(sources, batch_branch, default_branch),
        checks_step(repo_facts, batch_branch),
        {"kind": "push", "branch": batch_branch},
        {"kind": "pr_create", "title": _pr_title(sources), "body": _pr_body(sources),
         "head": batch_branch, "base": default_branch},
        {"kind": "wait_checks", "branch": batch_branch},
        {"kind": "merge", "squash": False, "method": "rebase", "delete_branch": True,
         "branch": batch_branch, "default_branch": default_branch},
        *_tail_steps(sources, batch_branch, task_paths),
    ]
