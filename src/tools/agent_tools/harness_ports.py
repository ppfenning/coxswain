"""The tools' side of `harness.decompose_ports`: the store and route calls a decompose run makes, built here.

`shell.py` is the composition root. It passes `decompose_ports()` to `harness.cli.main`, so the harness never
imports this package.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from agent_tools import draft_apply, follow_up_intake, route, run_store, store_cli
from harness.decompose_ports import DecomposePorts


def write_task(runs_dir: Path, path: Path, parts: tuple[str, ...], text: str) -> int:
    """Edge: `route file`'s own writer. The store row first, the file second, then the export baseline."""
    return route.write_filed_item(runs_dir, path, "task", parts, text)


def withdraw_tasks(runs_dir: Path, root: Path, initiative: str, rels: Sequence[str], whole: bool) -> list[str]:
    """Edge: the `rels` actually withdrawn; a file goes only after its store row is dropped, else it is named and kept.

    `whole` withdraws the initiative itself through `draft_apply.remove`, which drops every row; when that fails,
    each task is withdrawn on its own."""
    if whole and rels:
        store = draft_apply.Store(
            "store",
            lambda name: run_store.work_items(runs_dir, name),
            lambda name, task, state, by, expected: store_cli.set_state(runs_dir, name, task, state, by, expected),
        )
        work_dir = root / Path(rels[0]).parent.parent.parent
        if draft_apply.remove(work_dir, initiative, "decompose left a task missing", "decompose", store) == 0:
            for rel in rels:
                (root / rel).unlink(missing_ok=True)
            return list(rels)
    stored = {r["task_id"] for r in run_store.work_items(runs_dir, initiative)}
    done: list[str] = []
    for rel in rels:
        task = Path(rel).stem
        result = store_cli.set_state(runs_dir, initiative, task, "dropped", "decompose") if task in stored else None
        if task in stored and not isinstance(result, store_cli.StateSet):
            print(f"decompose: {task} not withdrawn: its store row was not dropped ({result})", file=sys.stderr)
            continue
        (root / rel).unlink(missing_ok=True)
        done.append(rel)
    return done


def resolve_repo(value: str, repos_root: Path, is_checkout: Callable[[Path], bool], home: str) -> str | None:
    """Pure: `route.resolve_repo`, with its refusal as None."""
    path = route.resolve_repo(value, repos_root, is_checkout, home)
    return None if isinstance(path, route.RepoRefused) else path


def decompose_ports() -> DecomposePorts:
    return DecomposePorts(
        write_task=write_task,
        # the queue rows `route edit` would see, read by the same path `route file` writes them through
        read_tasks=run_store.read_queue,
        withdraw_tasks=withdraw_tasks,
        resolve_repo=resolve_repo,
        is_checkout=route.is_git_checkout,
        file_follow_up=follow_up_intake.file_follow_up,
    )
