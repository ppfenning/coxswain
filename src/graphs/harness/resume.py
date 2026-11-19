"""Reuse what an earlier run already earned, so a fix does not cost the whole epic again.

Five epics ran against the same initiative while the runner was being
corrected. Each redid every task from scratch, including the ones whose
lifecycle had already produced an approved patch, and that rework was most of
the bill. A task's lifecycle result is a pure function of its inputs plus the
model's answers — once it has an approved patch, running it again buys a
second opinion at full price.

So every task's lifecycle result is saved beside the manifest, and a later run
may name an earlier one to resume from: a task whose saved result carries an
approved patch is reused and costs nothing; everything else runs. Reuse is by
task id within the same initiative — it says nothing about whether the patch
still APPLIES to a phase branch that may have moved, and it does not need to:
the harness applies and checks every reused patch exactly as it would a fresh
one, so a stale patch quarantines with git's own diagnosis rather than
slipping through.

What makes a result reusable is deliberately narrow and read off the record,
never inferred: the graph emits a `draft_pr_create` proposal only on an
approved verdict, and a patch has to exist to be applied.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["commit_staged", "current_umask", "load_result", "result_path", "reusable", "save_result", "stage_text", "write_atomic"]


def result_path(runs_dir: Path | str, run_id: str, phase: str, task: str) -> Path:
    """`<runs_dir>/<run_id>/tasks/<phase>/<task>.json` — one file per task per run."""
    return Path(runs_dir) / run_id / "tasks" / phase / f"{task}.json"


def reusable(result: Mapping[str, Any] | None) -> bool:
    """Pure: an approved lifecycle result — a `draft_pr_create` proposal — with a patch to apply."""
    if not isinstance(result, Mapping):
        return False
    patch = str((result.get("build") or {}).get("patch") or "")
    approved = any(
        isinstance(p, Mapping) and p.get("kind") == "draft_pr_create" for p in result.get("proposals") or []
    )
    return approved and bool(patch.strip()) and not (result.get("fix_loop") or {}).get("stopped")


def current_umask() -> int:
    """The process umask. It can only be read by setting it, so it is set to 0 and put straight back."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def stage_text(path: Path, text: str) -> Path:
    """Write `text` to a new file beside `path`, flushed to disk, and return it; `path` itself is not touched.

    The staged name starts with `.` and ends `.tmp`, so no reader's glob for `<task>.json` ever matches it."""
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o666 & ~current_umask())  # mkstemp makes it 0600; a plain write would honour the umask
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
    return Path(staged)


def commit_staged(staged: Path, path: Path) -> Path:
    """Put a staged file in `path`'s place in one step (`os.replace`): a reader sees the old file or the new, never half."""
    os.replace(staged, path)
    return path


def write_atomic(path: Path, text: str) -> Path:
    """Replace `path` with `text` atomically: a crash mid-write leaves the file as it was, never truncated."""
    return commit_staged(stage_text(path, text), path)


def save_result(result: Mapping[str, Any], *, runs_dir: Path | str, run_id: str, phase: str, task: str) -> Path:
    """Save a task's result, atomically: a run task record is an attempt the cap counts, so it is never left half written."""
    path = result_path(runs_dir, run_id, phase, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_atomic(path, json.dumps(dict(result), indent=2, default=str))


def load_result(runs_dir: Path | str, run_id: str, phase: str, task: str) -> dict[str, Any] | None:
    """The saved result, or None when there is none or it cannot be read."""
    path = result_path(runs_dir, run_id, phase, task)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
