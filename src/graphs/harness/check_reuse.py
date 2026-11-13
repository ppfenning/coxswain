"""Reuse a passing check result for the same tree and the same command.

Builds, reviews and lands run in different processes on one machine, so the
record is a file in the repository's git common directory, shared by every
worktree. A result is reusable only when the tree hash and the exact command
string both match, and only when it passed. A miss is never an error: any
git or ledger fault runs the check as usual.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from harness.checks import run_checks

LEDGER_NAME = "cox-check-ledger.jsonl"
MAX_ROWS = 2000
REUSED_EXCERPT = "reused: same tree and command passed earlier"
_GIT_TIMEOUT = 120


def ledger_key(tree: str, cmd: str) -> tuple[str, str]:
    return (tree, cmd)


def find_reusable(rows: Sequence[Mapping[str, Any]], tree: str, cmd: str) -> Mapping[str, Any] | None:
    """The newest row (latest in `rows`) for this key that passed; anything else is never reusable."""
    key = ledger_key(tree, cmd)
    return next(
        (
            row
            for row in reversed(rows)
            if ledger_key(row.get("tree"), row.get("cmd")) == key  # type: ignore[arg-type]
            and row.get("passed") is True
            and row.get("outcome") == "passed"
        ),
        None,
    )


def reused_result(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": row.get("name"),
        "cmd": row.get("cmd"),
        "passed": row.get("passed"),
        "outcome": row.get("outcome"),
        "exit_code": row.get("exit_code"),
        "counts": row.get("counts") or {},
        "reused": True,
        "cpu_seconds": 0.0,
        "cpu_seconds_saved": row.get("cpu_seconds", 0.0),
        "excerpt": REUSED_EXCERPT,
    }


def trim(rows: Sequence[Mapping[str, Any]], keep: int) -> list[Mapping[str, Any]]:
    return list(rows[-keep:]) if keep > 0 else []


def ledger_row(result: Mapping[str, Any], tree: str) -> dict[str, Any]:
    return {
        "tree": tree,
        "name": result.get("name"),
        "cmd": result.get("cmd"),
        "passed": result.get("passed"),
        "outcome": result.get("outcome"),
        "exit_code": result.get("exit_code"),
        "counts": result.get("counts") or {},
        "cpu_seconds": result.get("cpu_seconds", 0.0),
    }


def _git(worktree: Path, env: Mapping[str, str], *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT,
    )
    return done.stdout.strip()


def tree_of(worktree: Path) -> str | None:
    """Tree hash of the worktree's content: tracked plus untracked, not ignored. None on any git error.

    Stages into a copy of the index, so the real index is never touched. The tree
    objects are written to the object database, which is harmless.

    The copy is dated 1970-01-01T00:00:01, so git treats every entry as racily clean and
    hashes the file rather than trusting its stat data. A fresh copy dated now would let
    an edit that keeps a file's size and mtime second read as unchanged, and a stale
    passing result would be reused for content that never ran.
    """
    try:
        index = Path(worktree) / _git(worktree, os.environ, "rev-parse", "--git-path", "index")
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "index"
            if index.is_file():
                shutil.copyfile(index, copy)
                os.utime(copy, (1, 1))  # not 0: git skips the racy check for an index dated 0
            env = {**os.environ, "GIT_INDEX_FILE": str(copy)}
            _git(worktree, env, "add", "-A")
            return _git(worktree, env, "write-tree") or None
    except subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError:
        return None


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """Ledger rows oldest first. A missing file is empty; an unreadable one raises OSError."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return [row for row in map(_decode, text.splitlines()) if row is not None]


def _decode(line: str) -> dict[str, Any] | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _append_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """One write in append mode, so concurrent writers do not interleave their lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _rewrite(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    os.replace(temp, path)


def run_checks_reusing(
    worktree: Path,
    checks: Sequence[Mapping[str, Any]],
    *,
    ledger_dir: Path,
    **run_checks_kwargs: Any,
) -> list[dict[str, Any]]:
    """`run_checks`, skipping each check that already passed on this exact tree.

    `ledger_dir` is the git common directory. Only checks that missed are run, so
    with `log_dir` the log numbers follow the miss list, not the original list.
    """
    tree = tree_of(worktree)
    path = Path(ledger_dir) / LEDGER_NAME
    try:
        rows = [] if tree is None else _read_rows(path)
    except OSError:
        tree = None
    if tree is None:
        return run_checks(worktree, checks, **run_checks_kwargs)

    hits = [
        find_reusable(rows, tree, check["cmd"]) if check.get("name") and check.get("cmd") else None for check in checks
    ]
    misses = [check for check, hit in zip(checks, hits, strict=True) if hit is None]
    fresh = run_checks(worktree, misses, **run_checks_kwargs) if misses else []
    fresh_iter = iter(fresh)
    results = [reused_result(hit) if hit is not None else next(fresh_iter) for hit in hits]

    new_rows = [ledger_row(r, tree) for r in fresh if r.get("passed") is True and r.get("outcome") == "passed"]
    if new_rows:
        try:
            _append_rows(path, new_rows)
            if len(rows) + len(new_rows) > MAX_ROWS:
                _rewrite(path, trim([*rows, *new_rows], MAX_ROWS))
        except OSError:
            pass  # a ledger that cannot be written costs a rerun later, never a failed check
    return results
