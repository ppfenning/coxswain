"""Remove stale run directories under pytest's `pytest-of-<user>` temp directory."""

import os
import shutil
from collections.abc import Sequence
from pathlib import Path

DEFAULT_MAX_AGE_SECONDS = 6 * 60 * 60


def stale_run_dirs(
    entries: Sequence[tuple[str, float]],
    now: float,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> list[str]:
    """Names whose newest mtime is strictly older than the limit, oldest first."""
    stale = sorted((mtime, name) for name, mtime in entries if now - mtime > max_age_seconds)
    return [name for _, name in stale]


def _newest_mtime(path: Path) -> float:
    """Newest mtime of the directory and everything under it; vanished files are skipped."""
    newest = path.stat().st_mtime
    for root, dirs, files in os.walk(path):
        for child in (*dirs, *files):
            try:
                newest = max(newest, os.lstat(os.path.join(root, child)).st_mtime)
            except OSError:
                continue
    return newest


def sweep_pytest_tmp(
    temp_root: Path,
    user: str,
    now: float,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> list[str]:
    """Remove stale run directories from `<temp_root>/pytest-of-<user>`; return their names."""
    base = temp_root / f"pytest-of-{user}"
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    entries: list[tuple[str, float]] = []
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            entries.append((child.name, _newest_mtime(child)))
        except OSError:
            continue
    stale = stale_run_dirs(entries, now, max_age_seconds)
    for name in stale:
        shutil.rmtree(base / name, ignore_errors=True)
    return stale
