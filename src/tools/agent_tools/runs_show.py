"""`cox runs show <run>` and `cox runs log <run>`: a run's transcripts and its check logs, each from the run folder when
it holds that copy and from object storage (`artifacts_url`) when it does not.

`show` prints the transcripts and `log` the check logs. The local copies are the loose
`<runs_dir>/<run>-trace/*.jsonl` files and the `*.log` files under `<runs_dir>/<run>/checks/`; the stored ones are the
`application/x-ndjson` and `text/plain` items the run's step rows list. The choice is made per item (`plan`):

- A local copy is printed from the run folder. When a stored item has the same name (`transcript-<file>` for a trace
  file; for check `n` of a task, `<nn>-<check>.log` on that task's step row, phase and task matching) and its sha256
  differs, a note says so. A `scrubbed` item differs by design and gets no note.
- A stored item no local copy matches is read from the bucket and held to its row's size and sha256
  (`harness.artifacts.locate`).

So a run on the machine that ran it reads as it always did, a run whose folder is gone (deleted, or never fetched)
reads wholly from the bucket, and a folder that lost some files reads the rest from the bucket. Nothing is copied back.

`stored`, `marked`, `plan`, `local_order` and `header` are pure; the edges are `cli_runs._runs_show` and `_runs_log`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

__all__ = ["ERROR_FIELD", "MEDIA", "Copy", "Local", "Stored", "header", "local_order", "marked", "plan", "stored"]

Which = Literal["show", "log"]
# The media type each command prints, as `harness.artifacts.sources` labels it.
MEDIA: Mapping[str, str] = {"show": "application/x-ndjson", "log": "text/plain"}
WHAT: Mapping[str, str] = {"show": "transcripts", "log": "check logs"}
ERROR_FIELD = "artifacts_error"  # harness.artifacts.ERROR_FIELD, as runs_artifacts copies it
RUN_FOLDER, BUCKET = "run folder", "bucket"
# harness.artifacts._trace_name's folding, copied so no harness code loads here.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Stored:
    """One item a step row lists: the step, its artifact name, the item itself, and the row's phase and task."""

    step: str
    name: str
    item: Mapping[str, object]
    phase: str = ""
    task: str = ""


@dataclass(frozen=True)
class Local:
    """One copy in the run folder: its path from the runs directory, and the sha256 of its bytes."""

    rel: str
    sha256: str


@dataclass(frozen=True)
class Copy:
    """One thing to print: from the run folder (`rel`) or the bucket (`stored`), with any note about it."""

    label: str
    source: str
    rel: str = ""
    stored: Stored | None = None
    note: str = ""


def _name(key: str) -> str:
    return key.rpartition("/")[2].rpartition("@")[0]  # harness.artifacts.name_of, kept here so no harness code loads


def _items(row: Mapping[str, object]) -> list[Mapping[str, object]]:
    found = row.get("artifacts_json")
    return [i for i in cast("list[object]", found) if isinstance(i, Mapping)] if isinstance(found, list) else []


def stored(rows: Sequence[Mapping[str, object]], which: Which) -> tuple[Stored, ...]:
    """Pure. The items of the media `which` prints, in step-row order, then the order each row lists them."""
    return tuple(
        Stored(str(row.get("step") or ""), _name(str(item["key"])), item, str(row.get("phase") or ""),
               str(row.get("task_id") or ""))
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


def _local_key(rel: str, which: Which) -> tuple[object, ...] | None:
    """What a local copy is called among the stored items: a trace file's transcript name, or a task check's
    (phase, task, number)."""
    parts = rel.split("/")
    if which == "show":
        return ("show", "transcript-" + re.sub(r"_{2,}", "_", _UNSAFE.sub("-", parts[-1])))
    if len(parts) == 5 and parts[1] == "checks" and parts[4].removesuffix(".log").isdigit():
        return ("log", parts[2], parts[3], int(parts[4].removesuffix(".log")))
    return None  # a level check's log: no task step row lists it


def _stored_key(one: Stored, which: Which) -> tuple[object, ...] | None:
    if which == "show":
        return ("show", one.name)
    number = re.match(r"(\d+)-", one.name)
    return ("log", one.phase, one.task, int(number.group(1))) if number else None


def plan(run_id: str, which: Which, local: Sequence[Local], items: Sequence[Stored], marked: bool
         ) -> tuple[tuple[Copy, ...], str]:
    """Pure. What to print, and the sentence that says why there is nothing when there is nothing. Every local copy is
    printed from the run folder, in natural order, noted when a stored item of its name holds other bytes; then every
    stored item no local copy matches, from the bucket, in row order."""
    by_key: dict[tuple[object, ...], list[Stored]] = {}
    for one in items:
        if (k := _stored_key(one, which)) is not None:
            by_key.setdefault(k, []).append(one)
    copies: list[Copy] = []
    matched: set[tuple[object, ...]] = set()
    for rel in local_order([c.rel for c in local]):
        sha = next(c.sha256 for c in local if c.rel == rel)
        k = _local_key(rel, which)
        twins = by_key.get(k, []) if k is not None else []
        if k is not None and twins:
            matched.add(k)
        differ = [t for t in twins if t.item.get("sha256") != sha and not t.item.get("scrubbed")]
        note = "; ".join(f"note: {rel} differs from the bucket's {t.step} {t.name} (sha256 {sha[:12]} here, "
                         f"{str(t.item.get('sha256'))[:12]} there); printing the run folder's" for t in differ)
        copies.append(Copy(rel, RUN_FOLDER, rel=rel, note=note))
    copies += [Copy(f"{one.step} {one.name}", BUCKET, stored=one) for one in items
               if _stored_key(one, which) not in matched]
    if copies:
        return tuple(copies), ""
    why = ("and a step row records that its upload failed (cox runs artifacts shows it)" if marked
           else "and no step row lists any in object storage (is artifacts_url set?)")
    return (), f"{run_id}: no {WHAT[which]} in the run folder, {why}"


def header(label: str, source: str) -> str:
    """Pure. The line printed above each copy."""
    return f"== {label} ({source})"
