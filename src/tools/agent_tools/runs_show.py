"""`cox runs show <run>` and `cox runs log <run>`: a run's transcripts and its check logs, from the run folder when it
holds them and from object storage (`artifacts_url`) when it does not.

`show` prints the transcripts: the loose `<runs_dir>/<run>-trace/*.jsonl` files, else every `application/x-ndjson`
item the run's step rows list. `log` prints the check logs: every `*.log` under `<runs_dir>/<run>/checks/`, else every
`text/plain` item the rows list. The run folder wins whenever it holds any copy, so a run on the machine that ran it
reads as it always did. Once the folder is gone (deleted, or never fetched to this machine) the step rows are the
index and the bucket holds the bytes, each held to the size and sha256 its row recorded (`harness.artifacts.locate`).
Nothing is copied back.

`stored`, `marked`, `choose`, `local_order` and `header` are pure; the edges are `cli_runs._runs_show` and `_runs_log`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

__all__ = ["ERROR_FIELD", "MEDIA", "Choice", "Stored", "choose", "header", "local_order", "marked", "stored"]

Which = Literal["show", "log"]
# The media type each command prints, as `harness.artifacts.sources` labels it.
MEDIA: Mapping[str, str] = {"show": "application/x-ndjson", "log": "text/plain"}
WHAT: Mapping[str, str] = {"show": "transcripts", "log": "check logs"}
ERROR_FIELD = "artifacts_error"  # harness.artifacts.ERROR_FIELD, as runs_artifacts copies it


@dataclass(frozen=True)
class Stored:
    """One item a step row lists: the step, its artifact name, and the item itself."""

    step: str
    name: str
    item: Mapping[str, object]


@dataclass(frozen=True)
class Choice:
    """Where a command reads from: `local` paths, `bucket` items, or `none` with the sentence that says why."""

    source: Literal["local", "bucket", "none"]
    local: tuple[str, ...] = ()
    items: tuple[Stored, ...] = ()
    note: str = ""


def _name(key: str) -> str:
    return key.rpartition("/")[2].rpartition("@")[0]  # harness.artifacts.name_of, kept here so no harness code loads


def _items(row: Mapping[str, object]) -> list[Mapping[str, object]]:
    found = row.get("artifacts_json")
    return [i for i in cast("list[object]", found) if isinstance(i, Mapping)] if isinstance(found, list) else []


def stored(rows: Sequence[Mapping[str, object]], which: Which) -> tuple[Stored, ...]:
    """Pure. The items of the media `which` prints, in step-row order, then the order each row lists them."""
    return tuple(
        Stored(str(row.get("step") or ""), _name(str(item["key"])), item)
        for row in rows
        for item in _items(row)
        if item.get("key") and item.get("media") == MEDIA[which]
    )


def marked(rows: Sequence[Mapping[str, object]]) -> bool:
    """Pure. Whether any row records that its artifacts were not all written (`result_json.artifacts_error`)."""
    return any(isinstance(r.get("result_json"), Mapping) and cast("Mapping[str, object]", r["result_json"]).get(ERROR_FIELD)
               for r in rows)


def _natural(path: str) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path))


def local_order(paths: Sequence[str]) -> tuple[str, ...]:
    """Pure. Local copies in natural order, so `checks/p1/t1/2.log` comes before `10.log` and `build-2` before
    `build-10`."""
    return tuple(sorted(paths, key=_natural))


def choose(run_id: str, which: Which, local: Sequence[str], items: Sequence[Stored], marked: bool) -> Choice:
    """Pure. The run folder's copies when there are any; else the items the step rows list; else why there is nothing.
    `marked` is whether a row records an upload error (`artifacts_error`), which `cox runs artifacts` shows."""
    if local:
        return Choice("local", local=local_order(local))
    if items:
        return Choice("bucket", items=tuple(items))
    why = ("and a step row records that its upload failed (cox runs artifacts shows it)" if marked
           else "and no step row lists any in object storage (is artifacts_url set?)")
    return Choice("none", note=f"{run_id}: no {WHAT[which]} in the run folder, {why}")


def header(label: str, source: str) -> str:
    """Pure. The line printed above each copy."""
    return f"== {label} ({source})"
