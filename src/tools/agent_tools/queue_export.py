"""Pure export of queue rows to the intake/ and work/ trees, plus the edge that applies it.

`plan_export` takes the rows `run_store.read_queue` returned and the text currently on disk, and
says which files to write and which to delete to make the tree match the rows. It never touches a
filesystem; `export_files` is the edge that reads, plans, and writes. An `initiative.md` is written
when an initiative row holds it, and is never deleted, whatever `files_on_disk` holds for it.

The hand-edit guard holds a file only when nothing the harness wrote explains it. Every harness writer (the export,
`route file`, `route edit`, `route sync`'s issue line, the decompose launch's initiative.md, store-fill) records what it
wrote with `record_written`, and a file whose own frontmatter says what the export would write, only rendered
differently, is never a hand edit either. What the board export holds it lists in `runs/.export-held.json`, which the
chair reads to raise one needs-chair per held file and which its export commit leaves out.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import subprocess
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, NamedTuple

from agent_tools.queue_rows import _split_frontmatter, render_item, row_path

Row = dict[str, Any]
RowKey = tuple[str, str]  # (initiative, task_id): the store's key for a row's last-exported hash
FileStatus = Literal["missing", "in_sync", "behind", "edited"]

_LOG = logging.getLogger(__name__)

_TREES = ("intake", "work")
_KEEP_PREFIXES = ("intake/held/", "intake/done/")  # never deleted, whether or not a row names them
_PATH_KEYS = {"task": ("initiative", "phase", "task_id"), "initiative": ("initiative",), "intake": ("task_id",)}
_BASELINE_RECORD = "runs/.export-baseline.json"  # this workspace's own record of what its harness writers wrote; never shared
_HELD_RECORD = "runs/.export-held.json"  # the files the last board export held, for the chair's facts and its export commit
_HAND_EDITS = "runs/.hand-edits"  # hand text saved before the export overwrites it; outside work/ and intake/, which the export reads


class Export(NamedTuple):
    touched: list[str]
    skipped: list[str]


class Hashes(NamedTuple):
    """The store's last-exported hashes, read once, and the setter that records a new one."""

    stored: Mapping[RowKey, str]
    record: Callable[[str, str, str], bool]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def is_ahead_of_store(stored_hash: str | None, file_text: str) -> bool:
    """True when the store holds a hash for the row and the file text no longer hashes to it."""
    return stored_hash is not None and text_hash(file_text) != stored_hash


def blob_hash(text: str) -> str:
    """The git blob id `text` would have, so a file's content compares directly with the blob committed at HEAD."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def classify_file(file_hash: str | None, baselines: Collection[str], export_hash: str) -> FileStatus:
    """Pure: where a workspace file stands against what the export would now write.

    `baselines` are the hashes this workspace itself last produced for the file: the blob committed at HEAD and the
    text its own last export wrote. The per-row hash in the shared store is never one of them, since another machine
    records it. Missing is written; equal to the export is in_sync; equal to a baseline is behind and is rewritten
    silently; anything else is edited and left alone, including a file with no baseline at all."""
    if file_hash is None:
        return "missing"
    if file_hash == export_hash:
        return "in_sync"
    if file_hash in baselines:
        return "behind"
    return "edited"


def may_overwrite(disk_text: str | None, baselines: Collection[str], export_text: str) -> bool:
    """False only for a file the classifier calls edited: it exists and matches neither the export nor a baseline."""
    file_hash = None if disk_text is None else blob_hash(disk_text)
    return classify_file(file_hash, baselines, blob_hash(export_text)) != "edited"


HELD_REASON = "edited by hand: matches neither this workspace's committed or exported copy nor the export"


class Held(NamedTuple):
    """A file the export leaves alone: its workspace-relative path, the initiative its path names, and why."""

    path: str
    initiative: str
    reason: str


def initiative_of(path: str) -> str:
    """The `<initiative>` of `work/<initiative>/...`; "" for any other path, such as an intake file."""
    parts = path.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "work" else ""


def held_files(
    writes: Sequence[tuple[str, str]], on_disk: Mapping[str, str], baselines: Mapping[str, Collection[str]]
) -> list[Held]:
    """Pure: every planned write the export holds, in path order. The writer's own predicate, and the one place it
    lives: only a path in `writes` (its text differs from the export) is judged, and it is held when it is on disk
    and `may_overwrite` refuses it against its `baselines`. A file already equal to its export is never held."""
    return [
        Held(path, initiative_of(path), HELD_REASON)
        for path, text in sorted(writes)
        if path in on_disk and not may_overwrite(on_disk[path], baselines.get(path, ()), text)
    ]


def committed_blob(workspace: Path, path: str) -> str | None:
    """Edge: the blob id of `path` at the workspace's HEAD, None when it is untracked or git cannot answer."""
    try:
        done = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--verify", "--quiet", f"HEAD:./{path}"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    blob = done.stdout.strip()
    return blob if done.returncode == 0 and blob else None


def read_baseline_record(workspace: Path) -> dict[str, str]:
    """Edge: this workspace's record of the blob ids its exports last wrote, empty when it is absent or unreadable."""
    try:
        record = json.loads((workspace / _BASELINE_RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in record.items()} if isinstance(record, dict) else {}


def write_baseline_record(workspace: Path, record: Mapping[str, str]) -> None:
    """Edge: persist the workspace's baseline record; a failure only costs the fallback, so it is logged, not raised."""
    full = workspace / _BASELINE_RECORD
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(dict(sorted(record.items())), indent=1) + "\n")
    except OSError as exc:
        _LOG.warning("could not write the export baseline record %s: %s", full, exc)


def recorded_baselines(record: Mapping[str, str], target: Mapping[str, str], now_equal: Collection[str]) -> dict[str, str]:
    """Pure: `record` with each path in `now_equal` set to the blob id of its target text."""
    return {**record, **{path: blob_hash(target[path]) for path in now_equal}}


def workspace_relative(workspace: Path, path: Path | str) -> str | None:
    """Pure: `path` as the export names it, `work/...` or `intake/...` relative to `workspace`; None for any other path.
    An absolute path is taken relative to `workspace` as written, never resolved, so a symlinked home still matches."""
    raw = Path(path)
    if raw.is_absolute():
        rel = os.path.relpath(raw, workspace)
        if rel.startswith(".."):
            return None
        raw = Path(rel)
    parts = raw.parts
    return raw.as_posix() if len(parts) > 1 and parts[0] in _TREES and raw.suffix == ".md" else None


def record_written(workspace: Path, texts: Mapping[str, str]) -> None:
    """Edge: record each workspace-relative path's text as this workspace's baseline, so a file a harness writer just
    wrote is never held as a hand edit: the next export finds it behind and rewrites it from the store."""
    workspace = Path(workspace)
    record = read_baseline_record(workspace)
    updated = recorded_baselines(record, texts, list(texts))
    if updated != record:
        write_baseline_record(workspace, updated)


def record_written_paths(workspace: Path, paths: Iterable[Path | str]) -> None:
    """Edge: `record_written` for files already on disk, named absolute or workspace-relative. A path outside the
    workspace's work/ and intake/ trees, or one that cannot be read, is skipped: recording is never worth a failure."""
    workspace = Path(workspace)
    texts = {}
    for path in paths:
        rel = workspace_relative(workspace, path)
        if rel is None:
            continue
        try:
            texts[rel] = (workspace / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    if texts:
        record_written(workspace, texts)


def _fields_and_body(text: str) -> tuple[dict[str, Any], list[str]]:
    split = _split_frontmatter(text)
    fields, body = split if split is not None else ({}, text)
    return fields, body.splitlines()


def diff_summary(disk_text: str, store_text: str | None) -> str:
    """Pure: one line on how a hand-edited file differs from its export: the frontmatter keys that changed, in file
    order, then the body lines added and removed. "no store row" without store text, "no change" for equal texts."""
    if store_text is None:
        return "no store row"
    if disk_text == store_text:
        return "no change"
    (disk_fields, disk_body), (store_fields, store_body) = _fields_and_body(disk_text), _fields_and_body(store_text)
    keys = [*disk_fields, *(k for k in store_fields if k not in disk_fields)]
    changed = [k for k in keys if disk_fields.get(k) != store_fields.get(k)]
    ops = [op for op in SequenceMatcher(None, store_body, disk_body).get_opcodes() if op[0] != "equal"]
    added, removed = sum(j2 - j1 for _, _, _, j1, j2 in ops), sum(i2 - i1 for _, i1, i2, _, _ in ops)
    body = f"body +{added}/-{removed} lines"
    return f"{', '.join(changed)} changed; {body}" if changed else body


def hand_edit_save_path(path: str, blob: str) -> str:
    """Pure: where the hand text of `path` with blob id `blob` is saved. Under runs/, never under work/ or intake/:
    the export reads every markdown file in those trees as a work file, so a copy saved beside the file would be one."""
    return f"{_HAND_EDITS}/{path.replace('/', '__')}.{blob[:12]}.md"


def held_record(
    held: Sequence[Held], on_disk: Mapping[str, str], exported: Mapping[str, str], saved: Mapping[str, str] | None = None
) -> list[dict[str, str]]:
    """Pure: the rows of `runs/.export-held.json`: each held file with the blob id of the text it holds, so one
    needs-chair is raised per held content, and a new edit to the same file raises another, and a `diff_summary`
    against its `exported` text. A path in `saved` (to its saved copy) carries it as `saved_to` and outcome `saved`."""
    kept = saved or {}
    return [
        {
            **h._asdict(), "blob": blob_hash(on_disk[h.path]), "diff_summary": diff_summary(on_disk[h.path], exported.get(h.path)),
            "saved_to": kept.get(h.path, ""), "outcome": "saved" if h.path in kept else "",
        }
        for h in held if h.path in on_disk
    ]


def read_held_record(workspace: Path) -> list[dict[str, str]]:
    """Edge: the files the last board export held, as `held_record` wrote them; empty when absent or unreadable."""
    try:
        rows = json.loads((Path(workspace) / _HELD_RECORD).read_text())
    except (OSError, ValueError):
        return []
    keys = ("path", "initiative", "reason", "blob", "diff_summary", "saved_to", "outcome")  # an older record reads a missing key as ""
    return [
        {key: str(row.get(key) or "") for key in keys}
        for row in rows if isinstance(row, dict) and row.get("path")
    ] if isinstance(rows, list) else []


def write_held_record(workspace: Path, rows: Sequence[Mapping[str, str]]) -> None:
    """Edge: persist the held list; a failure costs only the needs-chair, so it is logged, not raised."""
    full = Path(workspace) / _HELD_RECORD
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(list(rows), indent=1) + "\n")
    except OSError as exc:
        _LOG.warning("could not write the export held record %s: %s", full, exc)


def own_export(path: str, text: str, stored: Row | None = None) -> str | None:
    """Pure: the text the export would write at `path` for the row `text` itself parses to. An `extra` key only the
    `stored` row carries (a `lint: []` the store added) is taken from it: a file that never named the key holds no
    edit of it. None when the file parses to no row, to a contentless one, or to a row that renders at another path."""
    from agent_tools.queue_rows import parse_item
    from agent_tools.store_fill import shape_of  # imported here: store_fill records through this module

    shape = shape_of(tuple(path.split("/")))
    row = None if shape is None else parse_item(*shape, text)
    if row is None or is_contentless(row):
        return None
    merged = {**row, "extra": {**((stored or {}).get("extra") or {}), **row["extra"]}}
    return export_rows([merged]).get(path)


def agreeing_blobs(
    writes: Sequence[tuple[str, str]], on_disk: Mapping[str, str], stored: Mapping[str, Row] | None = None
) -> dict[str, str]:
    """Pure: path to the blob id of its file, for each planned write whose file already says what the export would
    write, only rendered differently (key order, flow lists, a key the store alone added). Such a file holds no hand
    edit, whoever wrote it: a decompose that wrote the row and the file together, for one. `stored` is the row each
    path renders from."""
    rows = stored or {}
    return {
        path: blob_hash(on_disk[path])
        for path, text in writes
        if path in on_disk and own_export(path, on_disk[path], rows.get(path)) == text
    }


def rows_by_path(rows: Sequence[Row]) -> dict[str, Row]:
    """Pure: workspace-relative path to the row that renders it, for every row with content."""
    return {row_path(row): row for row in rows if not is_contentless(row)}


def store_hashes(runs_dir: Path) -> Hashes:
    """Edge: the run store's export hashes for `runs_dir`, empty when the store cannot be read."""
    from agent_tools import run_store  # imported here: the pure functions above need no store

    return Hashes(run_store.export_hashes(runs_dir), functools.partial(run_store.set_export_hash, runs_dir))


def is_contentless(row: Row) -> bool:
    """True for a row with nothing to render: no kind, or title, body and extra all None."""
    return row.get("kind") is None or all(row.get(key) is None for key in ("title", "body", "extra"))


def skipped_ids(rows: list[Row]) -> list[str]:
    """The task_id of each contentless row, in row order; `#<index in rows>` when it has none, so each stays distinct."""
    return [str(row.get("task_id") or f"#{i}") for i, row in enumerate(rows) if is_contentless(row)]


def _named_path(row: Row) -> str | None:
    """row_path when the kind and the fields it keys on are set, else None: a contentless row may still name its file."""
    keys = _PATH_KEYS.get(row.get("kind"))
    named = keys is not None and all(row.get(key) is not None for key in keys)
    return row_path({"state": None, **row}) if named else None


def _filled(row: Row) -> Row:
    """None in a content field becomes its empty value, so render_item never sees None where it expects text or a collection."""
    empty = {"title": "", "needs": [], "surfaces": [], "body": "", "extra": {}}
    return {**row, **{key: row.get(key) or value for key, value in empty.items()}}


def export_rows(rows: list[Row]) -> dict[str, str]:
    """Workspace-relative path to file text for every row with content: initiative, tickets and intake alike."""
    return {row_path(row): render_item(_filled(row)) for row in rows if not is_contentless(row)}


def plan_export(
    rows: list[Row], files_on_disk: dict[str, str], *, protected: frozenset[str] = frozenset()
) -> tuple[list[tuple[str, str]], list[str]]:
    """(writes, deletes) to bring `files_on_disk` to match `rows`. `render_item` is deterministic,
    so a path whose text already matches is left alone, and a board that changed nothing gives ([], []).
    A contentless row is not written, but the path it names is kept, never deleted.
    A file under a `protected` initiative is never deleted."""
    target = export_rows(rows)
    kept = {_named_path(row) for row in rows if is_contentless(row)}
    writes = [(path, text) for path, text in target.items() if files_on_disk.get(path) != text]
    deletes = [
        path
        for path in files_on_disk
        if path not in target
        and path not in kept
        and Path(path).name != "initiative.md"
        and not path.startswith(_KEEP_PREFIXES)
        and initiative_of(path) not in protected
    ]
    return writes, deletes


def _read_files(workspace: Path) -> dict[str, str]:
    roots = [workspace / tree for tree in _TREES if (workspace / tree).is_dir()]
    return {
        path.relative_to(workspace).as_posix(): path.read_text()
        for root in roots
        for path in root.rglob("*.md")
    }


def _prune_empty_dirs(directory: Path, stop_at: set[Path]) -> None:
    while directory not in stop_at and directory.is_dir() and not any(directory.iterdir()):
        parent = directory.parent
        directory.rmdir()
        directory = parent


def _row_keys(rows: list[Row]) -> dict[str, RowKey]:
    """Workspace-relative path to the store key of the row that renders it, for every row with content."""
    return {row_path(row): (str(row.get("initiative") or ""), str(row.get("task_id") or "")) for row in rows if not is_contentless(row)}


def hashes_to_record(target: dict[str, str], written: set[str], on_disk: dict[str, str], keys: dict[str, RowKey], stored: Mapping[RowKey, str]) -> list[tuple[RowKey, str]]:
    """(key, hash) for each path now holding its target text, just written or already equal, whose stored hash is not that text's."""
    now_equal = sorted(written | {path for path, text in target.items() if on_disk.get(path) == text})
    digests = {path: text_hash(target[path]) for path in now_equal}
    return [(keys[path], digest) for path, digest in digests.items() if stored.get(keys[path]) != digest]


def _save_hand_edit(workspace: Path, path: str, text: str) -> str | None:
    """Edge: save the hand `text` of `path` under runs/.hand-edits and return its workspace-relative path; the same blob
    already saved is a no-op. None, logged, when it cannot be written: the caller must then leave the file alone."""
    saved_to = hand_edit_save_path(path, blob_hash(text))
    full = workspace / saved_to
    try:
        if not full.is_file():
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text)
    except OSError as exc:
        _LOG.warning("could not save the hand edit of %s to %s, so the export keeps the file: %s", path, full, exc)
        return None
    return saved_to


def _saved_warning(workspace: Path, path: str, saved_to: str) -> str:
    return (
        f"{workspace / path} was edited by hand: it matches neither this workspace's last committed or exported copy "
        f"nor the store's export. Its text is saved at {workspace / saved_to} and the export has overwritten the file. "
        f"To keep the edit, copy the saved text back and `cox route import --workspace {workspace}` overwrites the store with the file."
    )


def _edited_warning(workspace: Path, path: str) -> str:
    return (
        f"{workspace / path} was edited by hand: it matches neither this workspace's last committed or exported copy "
        f"nor the store's export, so the export left it alone. Compare it first: git -C {workspace} diff HEAD -- {path}. "
        f"To keep the edit, `cox route import --workspace {workspace}` overwrites the store with the file."
    )


def _write_baselines(
    workspace: Path, record: Mapping[str, str], writes: Sequence[tuple[str, str]], on_disk: Mapping[str, str],
    stored: Mapping[str, Row],
) -> dict[str, set[str]]:
    """Edge: the baselines of each planned write already on disk: its blob at HEAD, the blob a harness writer last
    recorded for it, and its own blob when its frontmatter already agrees with the export."""
    agreeing = agreeing_blobs(writes, on_disk, stored)
    return {
        path: {blob for blob in (committed_blob(workspace, path), record.get(path), agreeing.get(path)) if blob}
        for path, _ in writes
        if path in on_disk
    }


def read_held(workspace: Path, rows: list[Row]) -> list[Held]:
    """Edge: the files `export_files` would hold for `rows` under `workspace`, all of them, without writing anything."""
    workspace = Path(workspace)
    on_disk = _read_files(workspace)
    writes, _ = plan_export(rows, on_disk)
    return held_files(
        writes, on_disk, _write_baselines(workspace, read_baseline_record(workspace), writes, on_disk, rows_by_path(rows))
    )


def _write_guarded(
    workspace: Path, target: dict[str, str], writes: list[tuple[str, str]], on_disk: dict[str, str],
    keys: dict[str, RowKey], hashes: Hashes | None, force: bool, by_path: Mapping[str, Row],
) -> tuple[list[str], list[Held], dict[str, str]]:
    """Edge: write `writes` under `workspace`. A file the classifier calls edited has its hand text saved under
    runs/.hand-edits first, then the export text written over it; when the save fails the file is left untouched and
    held, logged. A file behind this workspace's own baseline is rewritten silently. Records the hash of every file
    that now holds its target text, in the store and in the workspace's baseline record. Returns the paths written, the
    edited files, and the saved path of each that was saved. `by_path` is the row each path renders from. `hashes=None`
    is no guard."""
    stored = {} if hashes is None else hashes.stored
    record = {} if hashes is None else read_baseline_record(workspace)
    guarded = hashes is not None and not force
    held_list = held_files(writes, on_disk, _write_baselines(workspace, record, writes, on_disk, by_path)) if guarded else []
    attempts = {h.path: _save_hand_edit(workspace, h.path, on_disk[h.path]) for h in held_list}
    saved = {path: saved_to for path, saved_to in attempts.items() if saved_to is not None}
    held = {path for path, saved_to in attempts.items() if saved_to is None}
    for path in sorted(attempts):
        _LOG.warning("%s", _edited_warning(workspace, path) if path in held else _saved_warning(workspace, path, saved[path]))
    written = [(path, text) for path, text in writes if path not in held]
    for path, text in written:
        full = workspace / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)
    if hashes is not None:
        written_paths = {path for path, _ in written}
        for (initiative, task_id), digest in hashes_to_record(target, written_paths, on_disk, keys, stored):
            hashes.record(initiative, task_id, digest)
        now_equal = written_paths | {path for path, text in target.items() if on_disk.get(path) == text}
        updated = recorded_baselines(record, target, now_equal)
        if updated != record:
            write_baseline_record(workspace, updated)
    return [path for path, _ in written], held_list, saved


def _forget_held(workspace: Path, paths: Collection[str]) -> None:
    """Edge: drop `paths` from the held record, once a forced write has replaced them."""
    rows = read_held_record(workspace)
    kept = [row for row in rows if row["path"] not in paths]
    if kept != rows:
        write_held_record(workspace, kept)


def export_file(workspace: Path, row: Row, *, force: bool = False, hashes: Hashes | None = None) -> Export:
    """Edge: write the one file `row` renders under `workspace`, and record its hash. A hand-edited file is left
    untouched and logged unless `force`, which writes regardless. A contentless row writes nothing and is skipped."""
    if is_contentless(row):
        return Export([], skipped_ids([row]))
    workspace = Path(workspace)
    target = export_rows([row])
    [(path, text)] = target.items()
    on_disk = {path: (workspace / path).read_text()} if (workspace / path).is_file() else {}
    writes = [(path, text)] if on_disk.get(path) != text else []
    written, _, _ = _write_guarded(workspace, target, writes, on_disk, _row_keys([row]), hashes, force, rows_by_path([row]))
    if written:
        _forget_held(workspace, written)
    return Export(sorted(written), [])


def export_files(
    workspace: Path, rows: list[Row], *, hashes: Hashes | None = None, protected: frozenset[str] = frozenset()
) -> Export:
    """Edge: write and delete files under `workspace` so intake/ and work/ match `rows`.
    Returns the sorted paths touched and the ids of contentless rows skipped. Nothing under
    intake/held/ or intake/done/ is deleted. Rows with no content to export mean the store was
    unavailable or degraded, not an empty board, so the tree is left unchanged.
    With `hashes`, a hand-edited file has its text saved under `runs/.hand-edits` and is then rewritten, or is left
    untouched and logged when the save fails; one merely behind this workspace's own baseline is rewritten silently.
    Both kinds are listed in `runs/.export-held.json`, replacing the last list, so a file no longer edited drops out
    of it. Files of a `protected` initiative are never deleted."""
    skipped = skipped_ids(rows)
    if len(skipped) == len(rows):
        return Export([], skipped)
    workspace = Path(workspace)
    on_disk = _read_files(workspace)
    writes, deletes = plan_export(rows, on_disk, protected=protected)
    exported = export_rows(rows)
    written, held, saved = _write_guarded(
        workspace, exported, writes, on_disk, _row_keys(rows), hashes, False, rows_by_path(rows)
    )
    if hashes is not None:
        rows_held = held_record(held, on_disk, exported, saved)
        if rows_held != read_held_record(workspace):
            write_held_record(workspace, rows_held)
    stop_at = {workspace, workspace / "intake", workspace / "work"}
    for path in deletes:
        full = workspace / path
        full.unlink()
        _prune_empty_dirs(full.parent, stop_at)
    return Export(sorted(set(written) | set(deletes)), skipped)
