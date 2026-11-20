"""Trace store: one model call's JSON events, per run, zstd compressed.

Layout under a trace root: YYYY/MM/DD/<run_id>.parquet, one zstd Parquet file per run,
rewritten whole by write_run. Legacy YYYY/MM/DD/<run_id>.jsonl.zst day files still read
when a run has no Parquet file: each append is one zstd frame, and concatenated frames
are a valid zstd stream.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO, Protocol, TextIO, cast

from harness.store_dialect import JsonObject
from harness.trace_columns import COLUMNS, events_of, to_rows
from harness.traces_url import TracesRoot, have_pyarrow, resolve_traces_root

SUFFIX = ".jsonl.zst"
PARQUET_SUFFIX = ".parquet"


class ArrowFileInfo(Protocol):
    """What a pyarrow `FileInfo` shows to this module. pyarrow ships no type stubs, so the calls on it are typed here."""

    @property
    def path(self) -> str: ...

    @property
    def type(self) -> object: ...


class ArrowFs(Protocol):
    """The pyarrow filesystem calls this module makes."""

    def get_file_info(self, target: object, /) -> object: ...

    def open_input_file(self, path: str, /) -> AbstractContextManager[BinaryIO]: ...

    def create_dir(self, path: str, /, *, recursive: bool) -> None: ...

    def move(self, src: str, dest: str, /) -> None: ...


class ArrowTable(Protocol):
    """A pyarrow table, opaque here: built by `build_table`, written by `put_table`."""


class _ArrowRoot(Protocol):
    @property
    def fs(self) -> ArrowFs: ...


class _Compressor(Protocol):
    def compress(self, data: bytes, /) -> bytes: ...


class _Decompressor(Protocol):
    def stream_reader(self, source: BinaryIO, /, *, read_across_frames: bool) -> BinaryIO: ...


class _Zstd(Protocol):
    def ZstdCompressor(self) -> _Compressor: ...

    def ZstdDecompressor(self) -> _Decompressor: ...


def _fs(root: TracesRoot) -> ArrowFs:
    """The root's pyarrow filesystem, typed by what this module calls on it."""
    return cast(_ArrowRoot, root).fs


def is_local_root(root: TracesRoot) -> bool:
    """Whether the root is on the local filesystem."""
    return isinstance(_fs(root), importlib.import_module("pyarrow.fs").LocalFileSystem)


class TracesUnavailable(RuntimeError):
    """zstandard is not installed."""


class ParquetUnavailable(RuntimeError):
    """pyarrow is not installed, so a run cannot be written as Parquet."""


def day_dir(root: Path, day: str) -> Path:
    year, month, date = day.split("-")
    return Path(root) / f"{int(year):04d}" / f"{int(month):02d}" / f"{int(date):02d}"


def run_file(root: Path, day: str, run_id: str) -> Path:
    return day_dir(root, day) / f"{run_id}{SUFFIX}"


def run_parquet(root_path: str, day: str, run_id: str) -> str:
    year, month, date = day.split("-")
    return f"{root_path.rstrip('/')}/{int(year):04d}/{int(month):02d}/{int(date):02d}/{run_id}{PARQUET_SUFFIX}"


def to_lines(run_id: str, call_id: str, events: Sequence[JsonObject]) -> list[str]:
    """One JSONL row per event. seq counts from 0 in event order."""
    return [
        json.dumps({"run_id": run_id, "call_id": call_id, "seq": seq, "event": event}, sort_keys=True, separators=(",", ":"))
        for seq, event in enumerate(events)
    ]


def _zstd() -> _Zstd:
    try:
        zstandard = importlib.import_module("zstandard")
    except ImportError as exc:
        raise TracesUnavailable("reading or writing traces needs zstandard: install the traces extra") from exc
    return cast(_Zstd, zstandard)


def append_call(root: Path, day: str, run_id: str, call_id: str, events: Sequence[JsonObject]) -> Path:
    zstd = _zstd()
    path = run_file(root, day, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = zstd.ZstdCompressor().compress(("\n".join(to_lines(run_id, call_id, events)) + "\n").encode())
    with path.open("ab") as fh:
        fh.write(frame)
    return path


def read_legacy_file(path: Path) -> Iterator[JsonObject]:
    """Rows of one legacy day file in append order. `event` is a dict."""
    with Path(path).open("rb") as fh:
        reader = _zstd().ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        for line in io.TextIOWrapper(reader, encoding="utf-8"):
            if line.strip():
                yield cast(JsonObject, json.loads(line))


def _as_root(root: str | Path | TracesRoot) -> TracesRoot | None:
    """A plain path becomes a local root. None when pyarrow is missing, so there is no filesystem to build."""
    if isinstance(root, TracesRoot):
        return root
    return resolve_traces_root(str(root), Path("."), {}) if have_pyarrow() else None


def _legacy_dir(root: str | Path | TracesRoot) -> Path | None:
    """The directory legacy files live under. Local roots only."""
    if not isinstance(root, TracesRoot):
        return Path(root)
    return Path(root.path) if is_local_root(root) else None


def find_parquet(root: TracesRoot, run_id: str) -> list[str]:
    """Paths of <root>/YYYY/MM/DD/<run_id>.parquet, in date order."""
    pa_fs = importlib.import_module("pyarrow.fs")
    depth = len(root.path.strip("/").split("/")) + 4
    name = f"{run_id}{PARQUET_SUFFIX}"
    infos = cast(
        "list[ArrowFileInfo]",
        _fs(root).get_file_info(pa_fs.FileSelector(root.path, recursive=True, allow_not_found=True)),
    )
    return sorted(
        info.path
        for info in infos
        if info.type == pa_fs.FileType.File
        and len(parts := info.path.strip("/").split("/")) == depth
        and parts[-1] == name
        and all(part.isdigit() for part in parts[-4:-1])
    )


def parquet_rows(root: TracesRoot, path: str) -> list[JsonObject]:
    pq = importlib.import_module("pyarrow.parquet")
    with _fs(root).open_input_file(path) as fh:
        return cast("list[JsonObject]", pq.ParquetFile(fh).read().to_pylist())


def _from_parquet(root: str | Path | TracesRoot, run_id: str) -> list[JsonObject] | None:
    """Every Parquet row of a run, or None when the run has no Parquet file."""
    resolved = _as_root(root)
    if resolved is None:
        return None
    paths = find_parquet(resolved, run_id)
    return [row for path in paths for row in parquet_rows(resolved, path)] if paths else None


def _legacy_rows(root: str | Path | TracesRoot, run_id: str) -> Iterator[JsonObject]:
    base = _legacy_dir(root)
    for path in sorted(base.glob(f"*/*/*/{run_id}{SUFFIX}")) if base is not None else []:
        yield from read_legacy_file(path)


def iter_run(root: str | Path | TracesRoot, run_id: str) -> Iterator[JsonObject]:
    """Every row of a run: Parquet if the run has a file, else legacy day files in date order.

    Parquet rows carry every trace_columns column with `event` a JSON string. Legacy rows carry
    run_id, call_id, seq and `event` as a dict.
    """
    rows = _from_parquet(root, run_id)
    yield from rows if rows is not None else _legacy_rows(root, run_id)


def read_call(root: str | Path | TracesRoot, run_id: str, call_id: str) -> list[JsonObject]:
    rows = _from_parquet(root, run_id)
    if rows is not None:
        return cast("list[JsonObject]", events_of([r for r in rows if r["call_id"] == call_id]))
    legacy = sorted(
        (r for r in _legacy_rows(root, run_id) if r["call_id"] == call_id), key=lambda r: cast(int, r["seq"])
    )
    return [cast(JsonObject, r["event"]) for r in legacy]


def build_table(rows: Sequence[Mapping[str, object]]) -> ArrowTable:
    """Arrow table of COLUMNS with rows in call_id, seq order. seq is int32."""
    pa = importlib.import_module("pyarrow")
    types = {"string": pa.string(), "int32": pa.int32()}
    schema = pa.schema([(name, types[kind]) for name, kind in COLUMNS])
    return pa.Table.from_pylist(sorted(rows, key=lambda r: (cast(str, r["call_id"]), cast(int, r["seq"]))), schema=schema)


def put_table(root: TracesRoot, path: str, table: ArrowTable) -> None:
    pq = importlib.import_module("pyarrow.parquet")
    fs = _fs(root)
    if is_local_root(root):
        fs.create_dir(path.rpartition("/")[0], recursive=True)
        tmp = f"{path}.tmp"
        pq.write_table(table, tmp, compression="zstd", filesystem=fs)
        fs.move(tmp, path)
    else:
        pq.write_table(table, path, compression="zstd", filesystem=fs)


def write_run(root: TracesRoot, day: str, run_id: str, calls: Mapping[str, Sequence[JsonObject]]) -> int:
    """Write the run as one Parquet file; return the rows read back for `calls`, not the file total. A call already in the file is replaced."""
    try:
        importlib.import_module("pyarrow")
    except ImportError as exc:
        raise ParquetUnavailable("writing traces as Parquet needs pyarrow: install the traces extra") from exc
    pa_fs = importlib.import_module("pyarrow.fs")
    path = run_parquet(root.path, day, run_id)
    info = cast(ArrowFileInfo, _fs(root).get_file_info(path))
    kept = parquet_rows(root, path) if info.type == pa_fs.FileType.File else []
    new = [row for call_id, events in calls.items() for row in to_rows(run_id, call_id, day, events)]
    table = build_table([*(r for r in kept if r["call_id"] not in calls), *new])
    put_table(root, path, table)
    return sum(1 for r in parquet_rows(root, path) if r["call_id"] in calls)


def parse_argv(argv: Sequence[str]) -> tuple[str, str] | None:
    """(root, run_id) for exactly `dump <root> <run_id>`, else None."""
    return (argv[1], argv[2]) if len(argv) == 3 and argv[0] == "dump" else None


def dump_lines(rows: Sequence[Mapping[str, object]]) -> list[str]:
    """One JSON object per row in call_id, seq order. Keys follow COLUMNS; `event` stays a JSON string."""
    names = [name for name, _ in COLUMNS]
    ordered = sorted(rows, key=lambda r: (cast(str, r["call_id"]), cast(int, r["seq"])))
    return [json.dumps({name: row[name] for name in names}, ensure_ascii=False) for row in ordered]


def main(argv: Sequence[str], env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    """Exit 0 rows printed, 2 bad arguments or no pyarrow, 3 no Parquet file, 1 unreadable. Errors never print the root."""
    parsed = parse_argv(argv)
    if parsed is None:
        err.write("usage: python -m harness.store_traces dump <root> <run_id>\n")
        return 2
    root, run_id = parsed
    if not have_pyarrow():
        err.write("the traces extra is not installed\n")
        return 2
    try:
        resolved = resolve_traces_root(root, Path("."), env)
    except ValueError:
        err.write(f"cannot resolve the traces root for run {run_id}\n")
        return 2
    try:
        rows = _from_parquet(resolved, run_id)
    except OSError:
        err.write(f"cannot read traces for run {run_id}\n")
        return 1
    if rows is None:
        return 3
    out.write("".join(f"{line}\n" for line in dump_lines(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ, sys.stdout, sys.stderr))
