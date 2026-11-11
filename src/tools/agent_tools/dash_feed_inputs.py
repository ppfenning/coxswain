"""Fingerprints of the files the dash feed reads, one per input group, so a section can tell its inputs moved.

Wrong belief to avoid: "host beat times and chair rows are their own inputs". They live in the run store, so the
`store` group covers them and no group of their own exists.

Wrong belief to avoid: "`records` is every file under the runs dir that is not the store". The `<run>-trace/`
directories hold the lane traces, which have their own group, so `records` leaves them out.

Wrong belief to avoid: "every file under runs is a feed input". The feed's own share dir `.dash-feed/` and the
`<run>.progress.json` files are not read by any feed section, so the probe skips them and the walk never enters
`.dash-feed/`. Rewriting them must not move a group. Every other directory under runs or projects is read by a group.

`.ccusage-block.json` is the exception. `usage_window.gather` reads it for the spend section, so it stays in `records`
even though the feed also writes it.

A group fingerprint of None means always changed: the store URL is not a local sqlite file, so there is nothing to stat.
A caller that can stamp such a store (a Postgres change counter, say) passes `store_stamp`, and that stamp is the
`store` fingerprint. With no stamp the store stays None.
A fingerprint is a sorted tuple of (path, reading) pairs, where a reading is (size, mtime_ns) or UNREADABLE.
"""

from __future__ import annotations

import os
from collections.abc import Hashable, Iterator, Mapping
from pathlib import Path
from typing import Final

__all__ = ["GROUPS", "UNREADABLE", "changed", "classify", "fingerprint", "probe"]

GROUPS: Final = ("store", "records", "transcripts", "lane_traces")
UNREADABLE: Final = "unreadable"  # a str, so no (size, mtime_ns) pair can equal it
_STORE_NAMES: Final = frozenset({"cox.db", "cox.db-wal", "cox.db-shm", "cox.db-journal"})
_TRACE_SUFFIX: Final = "-trace"  # lane traces are `<run>-trace/<role>-<n>.jsonl` under the runs dir
_SHARE_DIR: Final = ".dash-feed"  # the feed's own output dir under the runs dir
_PROGRESS_SUFFIX: Final = ".progress.json"  # written by the runs, read by no feed section

Reading = tuple[int, int] | str
Fingerprints = Mapping[str, Hashable | None]


def _prefix(base: Path) -> str:
    return str(base).rstrip(os.sep) + os.sep


def _group_of(path: str, runs_prefix: str, projects_prefix: str) -> str | None:
    """The group of one path string, decided by prefix checks alone."""
    if path.startswith(projects_prefix):
        return "transcripts"
    if not path.startswith(runs_prefix):
        return None
    parts = path[len(runs_prefix) :].split(os.sep)
    if len(parts) == 1 and parts[0] in _STORE_NAMES:
        return "store"
    if len(parts) > 1 and parts[0].endswith(_TRACE_SUFFIX):
        return "lane_traces"
    return "records"


def classify(path: Path, runs_dir: Path, projects_dir: Path) -> str | None:
    """The group a file belongs to, None when it is under neither directory."""
    return _group_of(str(path), _prefix(runs_dir), _prefix(projects_dir))


def fingerprint(
    readings: Mapping[str, Reading],
    runs_dir: Path,
    projects_dir: Path,
    store_is_local_sqlite: bool = True,
    store_stamp: Hashable | None = None,
) -> dict[str, Hashable | None]:
    """One fingerprint per group. An absent file is simply not in `readings`, which moves its group."""
    runs_prefix, projects_prefix = _prefix(runs_dir), _prefix(projects_dir)
    labelled = [(_group_of(path, runs_prefix, projects_prefix), path, reading) for path, reading in readings.items()]
    grouped = {
        group: tuple(sorted((path, reading) for label, path, reading in labelled if label == group)) for group in GROUPS
    }
    return {
        group: (store_stamp if group == "store" and not store_is_local_sqlite else grouped[group]) for group in GROUPS
    }


def changed(before: Fingerprints, after: Fingerprints) -> frozenset[str]:
    """The groups that moved. A None fingerprint on either side, or a missing group, counts as moved."""
    return frozenset(
        group
        for group in GROUPS
        if before.get(group) is None or after.get(group) is None or before[group] != after[group]
    )


def _skipped(name: str) -> bool:
    return name.endswith(_PROGRESS_SUFFIX)


def _files(base: Path, own_outputs: bool) -> Iterator[str]:
    """Edge: every input file under `base`. With `own_outputs`, the share dir is pruned and progress files skipped."""
    share = os.path.join(str(base), _SHARE_DIR)
    for root, dirs, names in os.walk(base):
        if own_outputs:
            dirs[:] = [name for name in dirs if os.path.join(root, name) != share]
        yield from (os.path.join(root, name) for name in names if not (own_outputs and _skipped(name)))


def probe(
    runs_dir: Path, projects_dir: Path, store_is_local_sqlite: bool = True, store_stamp: Hashable | None = None
) -> dict[str, Hashable | None]:
    """Edge: stat every input file under both directories. A path that cannot be stat'ed reads as UNREADABLE."""

    def read(path: str) -> Reading:
        try:
            info = os.stat(path)
        except OSError:
            return UNREADABLE
        return (info.st_size, info.st_mtime_ns)

    paths = [*_files(runs_dir, True), *_files(projects_dir, False)]
    return fingerprint({path: read(path) for path in paths}, runs_dir, projects_dir, store_is_local_sqlite, store_stamp)
