"""Fingerprints of the files the dash feed reads, one per input group, so a section can tell its inputs moved.

Wrong belief to avoid: "host beat times and chair rows are their own inputs". They live in the run store, so the
`store` group covers them and no group of their own exists.

Wrong belief to avoid: "`records` is every file under the runs dir that is not the store". The `<run>-trace/`
directories hold the lane traces, which have their own group, so `records` leaves them out.

A group fingerprint of None means always changed: the store URL is not a local sqlite file, so there is nothing to stat.
A fingerprint is a sorted tuple of (path, reading) pairs, where a reading is (size, mtime_ns) or UNREADABLE.
"""

from __future__ import annotations

import os
from collections.abc import Hashable, Mapping
from pathlib import Path
from typing import Final

__all__ = ["GROUPS", "UNREADABLE", "changed", "classify", "fingerprint", "probe"]

GROUPS: Final = ("store", "records", "transcripts", "lane_traces")
UNREADABLE: Final = "unreadable"  # a str, so no (size, mtime_ns) pair can equal it
_STORE_NAMES: Final = frozenset({"cox.db", "cox.db-wal", "cox.db-shm", "cox.db-journal"})
_TRACE_SUFFIX: Final = "-trace"  # lane traces are `<run>-trace/<role>-<n>.jsonl` under the runs dir

Reading = tuple[int, int] | str
Fingerprints = Mapping[str, Hashable | None]


def classify(path: Path, runs_dir: Path, projects_dir: Path) -> str | None:
    """The group a file belongs to, None when it is under neither directory."""
    if path.is_relative_to(projects_dir):
        return "transcripts"
    if not path.is_relative_to(runs_dir):
        return None
    parts = path.relative_to(runs_dir).parts
    if len(parts) == 1 and parts[0] in _STORE_NAMES:
        return "store"
    if len(parts) > 1 and parts[0].endswith(_TRACE_SUFFIX):
        return "lane_traces"
    return "records"


def fingerprint(
    readings: Mapping[str, Reading], runs_dir: Path, projects_dir: Path, store_is_local_sqlite: bool = True
) -> dict[str, Hashable | None]:
    """One fingerprint per group. An absent file is simply not in `readings`, which moves its group."""
    grouped = {
        group: tuple(
            sorted(
                (path, reading)
                for path, reading in readings.items()
                if classify(Path(path), runs_dir, projects_dir) == group
            )
        )
        for group in GROUPS
    }
    return {group: (None if group == "store" and not store_is_local_sqlite else grouped[group]) for group in GROUPS}


def changed(before: Fingerprints, after: Fingerprints) -> frozenset[str]:
    """The groups that moved. A None fingerprint on either side, or a missing group, counts as moved."""
    return frozenset(
        group
        for group in GROUPS
        if before.get(group) is None or after.get(group) is None or before[group] != after[group]
    )


def probe(runs_dir: Path, projects_dir: Path, store_is_local_sqlite: bool = True) -> dict[str, Hashable | None]:
    """Edge: stat every file under both directories. A path that cannot be stat'ed reads as UNREADABLE."""

    def read(path: str) -> Reading:
        try:
            info = os.stat(path)
        except OSError:
            return UNREADABLE
        return (info.st_size, info.st_mtime_ns)

    paths = [
        os.path.join(root, name)
        for base in (runs_dir, projects_dir)
        for root, _, names in os.walk(base)
        for name in names
    ]
    return fingerprint({path: read(path) for path in paths}, runs_dir, projects_dir, store_is_local_sqlite)
