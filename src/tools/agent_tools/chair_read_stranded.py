"""Chair reader for `stranded`: the rows of `runs_stranded.stranded`, projected to six keys."""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_tools import epic, land_manual, remote_lane, repo_path, runs_stranded

KEYS = ("run", "task", "phase", "branch", "remedy", "expected")


def keep_stranded_keys(rows: list[dict]) -> list[dict]:
    """Plain dicts holding only `KEYS`, in input order. `expected` is what the producer set; `None` means it did not say."""
    return [{k: row.get(k) for k in KEYS} for row in rows]


def _local_path(repo: str) -> str:
    """Edge. A recorded repo as an absolute path under this machine's home."""
    return repo_path.expand(repo, os.path.expanduser("~"))


def _is_manual(repo: str) -> bool:
    """Edge. Whether the recorded repo, expanded to this machine's home, is `land: manual`."""
    return land_manual.is_manual(_local_path(repo))


def _repo_exists(repo: str) -> bool:
    """A real directory check on the recorded repo, expanded to this machine's home."""
    return os.path.isdir(_local_path(repo))


def _branch_exists(repo: str, branch: str) -> bool:
    """A real, git-backed check: whether `branch` is present as a local branch of `repo`, expanded to this machine's home."""
    r = subprocess.run(["git", "-C", _local_path(repo), "branch", "--list", branch], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _read_log_texts(runs_dir: Path | str | None) -> dict[str, str]:
    """Edge. Run id to log text for each fetched run under `runs_dir`; a landed-elsewhere run, or a missing or unreadable log, is left out. `None` reads nothing."""
    if runs_dir is None:
        return {}
    root = Path(runs_dir)
    texts: dict[str, str] = {}
    for marker in sorted(root.glob("*.fetched.json")):
        run = marker.name.removesuffix(".fetched.json")
        try:
            if not remote_lane.is_landed_elsewhere_marker(marker.read_text(encoding="utf-8")):
                texts[run] = (root / f"{run}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return texts


def _log_row_is_stale(run: str, item: dict) -> bool:
    """True for an item the record path also skips: rebuilt from this run, or sent back by a land conflict."""
    return runs_stranded._rebuilt({"run": run}, item) or runs_stranded._awaiting_rebuild(item)


def _log_rows(run: str, text: str, items: list[dict], recorded: set[tuple[str, str]],
              repo_exists: Callable[[str], bool], manual_of: Callable[[str], bool]) -> list[dict]:
    """Rows for one run's log approvals. The repo is the item's own, so no record is needed. A task with any record, or in two repos, yields none: the record path decides it. A `land: manual` repo gets its own remedy."""
    lines = epic.summarize_log(text)["approved"]
    found = [(repo, row) for repo in sorted({i["repo"] for i in items if i.get("repo")})
             for row in runs_stranded.stranded_from_log(run, lines, items, repo)]
    per_task = Counter(row["task"] for _, row in found)
    return [
        {**row, "remedy": land_manual.REMEDY if manual_of(repo) else row["remedy"], "expected": bool(manual_of(repo))}
        for repo, row in found
        if per_task[row["task"]] == 1 and (run, row["task"]) not in recorded and repo_exists(repo)
        and not _log_row_is_stale(run, next(i for i in items if i.get("repo") == repo and i.get("id") == row["task"]))
    ]


def read_stranded(records: list[dict], items: list[dict],
                   repo_exists: Callable[[str], bool] = _repo_exists,
                   branch_exists: Callable[[str, str], bool] = _branch_exists,
                   log_texts: Mapping[str, str] | None = None,
                   runs_dir: Path | str | None = None) -> list[dict]:
    """Edge. `records` and `items` come from the caller: their loaders live in
    cli.py, which this module does not import. `repo_exists` defaults to a
    real directory check, on the recorded path expanded to this machine's
    home, so a dropped item or a vanished repository never
    reaches the chair planner as needs_chair; `branch_exists` defaults to a
    real git check so a task on a still-carried phase branch stops reaching
    the chair as a stranded quarantine; tests inject fakes for both.
    `log_texts` maps a run id to its log text and is the pure seam for the
    log approvals; when `None` the texts are read from `runs_dir` for fetched
    runs. `runs_dir=None` reads no log, and no caller passes it yet: wiring it
    in cli.py is a separate task, so until then this reader changes nothing in
    the chair tick. A task with any record is decided by the record path alone."""
    manual_of = _is_manual
    from_records = runs_stranded.stranded(records, items, repo_exists, branch_exists, manual_of=manual_of)
    texts = _read_log_texts(runs_dir) if log_texts is None else log_texts
    recorded = {(r.get("run"), r.get("task")) for r in records}
    from_logs = [row for run, text in texts.items()
                 for row in _log_rows(run, text, items, recorded, repo_exists, manual_of)]
    return keep_stranded_keys(from_records + from_logs)


def read_missing_repos(records: list[dict], items: list[dict],
                        repo_exists: Callable[[str], bool] = _repo_exists) -> list[str]:
    """Edge. The repo paths `read_stranded` is skipping because `repo_exists` rejects them."""
    return runs_stranded.missing_repos(records, items, repo_exists)


def stranded_from_rows(rows: list[dict], records: list[dict],
                       manual_of: Callable[[str], bool] = _is_manual) -> list[dict]:
    """The same rule as `read_stranded`, with each task's state taken from `rows` (kind "task") instead of files.

    `manual_of` reads the record's own repo, as in `read_stranded`; the items built here carry none."""
    items = [
        {
            "id": row.get("task_id"), "initiative": row.get("initiative"), "phase": row.get("phase"), "state": row.get("state"),
            "land_conflict": (row.get("extra") or {}).get("land_conflict"),
            "rebuilt_from": (row.get("extra") or {}).get("rebuilt_from"),
        }
        for row in rows
        if row.get("kind") == "task"
    ]
    return keep_stranded_keys(runs_stranded.stranded(records, items, manual_of=manual_of))
