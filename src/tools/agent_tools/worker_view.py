"""A worker's view of the initiative it claimed a phase of, read from the store and written where only its own launch
reads it.

Workers keep no state: the store holds the work, git holds the code. A worker's launch (`cox route launch epic
--lane-worker`) and the lane it starts (`phase-lane`) read an initiative as a directory, `initiative.md` and one file
per task, which on the chair machine the chair's export keeps under `work/`. No other machine has that directory, so the
launch writes the same files from the store rows (`queue_export.export_rows`, the export's own rendering) into a
directory of the run's own, beside its launch output, and points the lane at that: the same input on every machine,
the chair's included. It never writes the workspace's `work/`, whose files the store fill reads back as rows.

`stub_files` decides what goes in, `write_stub` writes it, `read_stub` reads the store and does both.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import queue_export, run_store
from agent_tools.route_plan import initiative_repo

STUB_SUFFIX = ".work"  # `<workspace>/worker-launches/<run id>.work/work/<initiative>/...`


def stub_rows(rows: Sequence[Mapping[str, Any]], initiative: str) -> list[dict[str, Any]]:
    """Pure. The rows a lane of `initiative` reads: its own initiative and task rows, and each task row of another
    initiative that one of its tasks needs (`workstore.read_initiative` reads those for their state, so a need across
    initiatives is not taken for a dangling one). Every other row, intake included, is left out."""
    own = [dict(r) for r in rows if r.get("initiative") == initiative and r.get("kind") in ("initiative", "task")]
    needed = {str(n) for r in own if r.get("kind") == "task" for n in (r.get("needs") or [])}
    held = {str(r.get("task_id")) for r in own if r.get("kind") == "task"}
    foreign = [dict(r) for r in rows if r.get("kind") == "task" and r.get("initiative") != initiative
               and str(r.get("task_id")) in needed - held]
    return own + foreign


def stub_files(rows: Sequence[Mapping[str, Any]], initiative: str) -> dict[str, str] | None:
    """Pure. Workspace-relative path to text for every file of `initiative`'s view (`stub_rows`), as the chair's export
    renders them; None when the rows hold no initiative row for it, whose lane would have no goal to read."""
    chosen = stub_rows(rows, initiative)
    if not any(r.get("kind") == "initiative" for r in chosen):
        return None
    return {path: text for path, text in queue_export.export_rows(chosen).items() if path.startswith("work/")}


def stub_root(workspace: Path, run_key: str) -> Path:
    """Pure. The directory the view of the run `run_key` is written to: beside that run's launch output."""
    return workspace / "worker-launches" / f"{run_key}{STUB_SUFFIX}"


def write_stub(root: Path, files: Mapping[str, str]) -> None:
    """Edge. `root` holding exactly `files`: whatever an earlier launch of the same run left there is removed first, so
    a second write of the same rows gives the same tree."""
    shutil.rmtree(root, ignore_errors=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def read_stub(runs_dir: Path, initiative: str, run_key: str) -> tuple[Path | None, str, str]:
    """Edge. Write the view of `initiative` for the run `run_key` from the store; `(initiative dir, repo, refusal)`.

    The directory is `<stub root>/work/<initiative>`, and `repo` is the one the rows name (`route.initiative_repo`: the
    initiative's own, else its intake's), "" for none. With no initiative row, or a store that does not answer
    (`read_queue` reads both as no rows), the directory is None and `refusal` says so; nothing is written then."""
    rows = run_store.read_queue(runs_dir)
    files = stub_files(rows, initiative)
    if files is None:
        return None, "", f"routing: no initiative row {initiative} in the store, or the store is unavailable"
    root = stub_root(runs_dir.parent, run_key)
    write_stub(root, files)
    head = next(r for r in rows if r.get("initiative") == initiative and r.get("kind") == "initiative")
    return root / "work" / initiative, initiative_repo(head, [r for r in rows if r.get("kind") == "intake"]), ""
