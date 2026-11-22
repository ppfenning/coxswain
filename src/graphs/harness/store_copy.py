"""Copy every table's rows from one store to another, idempotently, and check the counts.

    COX_STORE_COPY_SRC_URL=... COX_STORE_COPY_DST_URL=... python -m harness.store_copy [--json]
    python -m harness.store_copy SRC_URL DST_URL [--json]

The URLs are read from the environment first (`SRC_URL_ENV`, `DST_URL_ENV`): an argv is readable by every user on
the host through `ps`, an environment only by its owner, so a URL holding a password belongs there. A URL given on the
command line is still taken, for a store without a password.

Both sides are opened with `open_store_exact`, so the destination is migrated first, and a side newer than this
code is refused: the copy carries only the tables and columns this code knows, so a newer store's would be dropped. Rows
of an append-only table go in with insert-ignore: a destination row with the same key
but different content is left alone. Rows of a table in `_REFRESH` go in with upsert,
so a second run overwrites them and reports them as refreshed. No run inserts a row twice.
`schema_version` is not copied: each side migrates itself. The source is only read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

import harness.store_ddl_0001 as ddl1
import harness.store_ddl_0002 as ddl2
import harness.store_ddl_0003 as ddl3
import harness.store_ddl_0004 as ddl4
import harness.store_ddl_0005 as ddl5
import harness.store_ddl_0006 as ddl6
import harness.store_ddl_0007 as ddl7
import harness.store_ddl_0008 as ddl8
import harness.store_ddl_0009 as ddl9
import harness.store_ddl_0010 as ddl10
import harness.store_ddl_0011 as ddl11
import harness.store_ddl_0012 as ddl12
import harness.store_ddl_0013 as ddl13
import harness.store_ddl_0014 as ddl14
import harness.store_ddl_0015 as ddl15
import harness.store_ddl_0016 as ddl16
import harness.store_ddl_0017 as ddl17
import harness.store_ddl_0018 as ddl18
import harness.store_ddl_0019 as ddl19
import harness.store_ddl_0020 as ddl20
import harness.store_ddl_0021 as ddl21
import harness.store_ddl_0023 as ddl23
from harness.store_dialect import Connection, insert_ignore, upsert
from harness.store_migrate import NotExactVersion, open_store_exact

Table = tuple[str, tuple[str, ...], tuple[str, ...]]

# Parents before children: the registry, then runs, then what hangs off a run.
_ORDER = (
    "graphs",
    "graph_nodes",
    "graph_edges",
    "runs",
    "phases",
    "tasks",
    "task_records",
    "work_items",
    "attempts",
    "node_calls",
    "gate_decisions",
    "ledger",
    "leases",
    "chair_actions",
    "hosts",
    "id_sequence",
    "landing_records",
    "land_queue",
    "courier_log",
    "cartridge_snapshots",
    "lander_notices",
    "phase_queue",
    "step_results",
    "review_rounds",
    "workers",
    "chair_queue",
)

# Tables whose rows change after they are first written. Every other table in _ORDER is append-only.
_REFRESH = frozenset(
    {"runs", "phases", "tasks", "task_records", "work_items", "leases", "hosts", "id_sequence", "landing_records",
     "land_queue", "lander_notices", "phase_queue", "workers", "chair_queue"}
)


class CopyCheckFailed(Exception):
    """A destination table holds fewer rows than the source had. `report` is the full copy report."""

    def __init__(self, report: dict[str, dict[str, int]], short: dict[str, int]) -> None:
        super().__init__(f"destination is short of the source in: {', '.join(sorted(short))}")
        self.report = report
        self.short = short


def tables() -> tuple[Table, ...]:
    """(name, column names, key) for every table the DDL modules create, migration ALTER columns included."""
    # The list must follow every migration's ALTERs: a column left out is dropped silently and the copy holds NULL.
    alters = (
        *ddl2.ADDED,
        *ddl3.ADDED,
        *ddl5.ADDED,
        *ddl9.ADDED,
        *ddl10.ADDED,
        *ddl11.ADDED,
        *ddl12.ADDED,
        *ddl13.ADDED,
        *ddl14.ADDED,
        *ddl15.ADDED,
        *ddl20.ADDED,
        *ddl23.ADDED,
    )
    added = {t: tuple(c for u, c in alters if u == t) for t, _ in alters}
    return tuple(
        (name, (*(c for c, _ in columns), *added.get(name, ())), key)
        for name, columns, key in (
            *ddl1.TABLES,
            *ddl2.TABLES,
            *ddl4.TABLES,
            *ddl6.TABLES,
            *ddl7.TABLES,
            *ddl8.TABLES,
            *ddl11.TABLES,
            *ddl16.TABLES,
            *ddl17.TABLES,
            *ddl18.TABLES,
            *ddl19.TABLES,
            *ddl20.TABLES,
            *ddl21.TABLES,
        )
    )


def plan(known: Sequence[Table]) -> tuple[Table, ...]:
    """`known` in copy order. A table the order does not name is an error, so none is dropped silently."""
    unnamed = sorted(name for name, _, _ in known if name not in _ORDER)
    if unnamed:
        raise ValueError(f"no copy order for table(s): {', '.join(unnamed)}")
    by_name = {t[0]: t for t in known}
    return tuple(by_name[n] for n in _ORDER if n in by_name)


def _safe_url(url: str) -> str:
    """Scheme and path only: userinfo, host, port and query never reach output."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"{url.partition(':')[0]}://"
    return f"{parts.scheme}://{parts.path}"


SRC_URL_ENV = "COX_STORE_COPY_SRC_URL"
DST_URL_ENV = "COX_STORE_COPY_DST_URL"


def copy_urls(src_arg: str | None, dst_arg: str | None, environ: Mapping[str, str]) -> tuple[str, str] | None:
    """Pure. The (source, target) URLs: each from the environment if set there, else from the command line; None
    when either is missing from both."""
    src, dst = environ.get(SRC_URL_ENV) or src_arg, environ.get(DST_URL_ENV) or dst_arg
    return (src, dst) if src and dst else None


def _count(conn: Connection, table: str) -> int:
    row = conn.query_one(f"SELECT COUNT(*) FROM {table}")
    return 0 if row is None else int(cast(int, row[0]))


def _copy_table(
    src: Connection, dst: Connection, table: str, columns: tuple[str, ...], key: tuple[str, ...], batch: int
) -> tuple[int, int, int]:
    """(rows read from src, rows inserted into dst, rows refreshed). One destination transaction per batch.

    Refreshed counts the rows written over an existing key, changed or not.
    """
    p = src.dialect.placeholder
    select = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(key)} LIMIT {p} OFFSET {p}"
    statement = (upsert if table in _REFRESH else insert_ignore)(dst.dialect, table, columns, key)
    before = _count(dst, table)
    read = written = 0
    while True:
        rows = src.query_all(select, (batch, read))
        with dst.transaction():
            written += sum(dst.execute(statement, row) for row in rows)
        read += len(rows)
        if len(rows) < batch:
            inserted = _count(dst, table) - before
            return read, inserted, written - inserted


def copy(src_url: str, dst_url: str, now: str, batch: int = 500) -> dict[str, dict[str, int]]:
    """Copy every table, then check each destination count is at least the source count.

    Raises CopyCheckFailed, carrying the report, when one is short.
    """
    if batch < 1:
        raise ValueError(f"batch must be at least 1, got {batch}")
    src = open_store_exact(src_url, now, "source")
    try:
        dst = open_store_exact(dst_url, now, "destination")
        try:
            report: dict[str, dict[str, int]] = {}
            for name, columns, key in plan(tables()):
                read, inserted, refreshed = _copy_table(src, dst, name, columns, key, batch)
                report[name] = {"source": read, "copied": inserted, "refreshed": refreshed, "present": read - inserted}
            held = {n: _count(dst, n) for n in report}
            short = {n: c for n, c in held.items() if c < report[n]["source"]}
        finally:
            dst.close()
    finally:
        src.close()
    if short:
        raise CopyCheckFailed(report, short)
    return report


def _lines(report: dict[str, dict[str, int]]) -> list[str]:
    return [
        f"{n} source={r['source']} copied={r['copied']} refreshed={r['refreshed']} present={r['present']}"
        for n, r in report.items()
    ]


def main(argv: Sequence[str], environ: Mapping[str, str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.store_copy", description=cast(str, __doc__).split("\n")[0])
    ap.add_argument("src_url", nargs="?", help=f"the source store, if {SRC_URL_ENV} is not set")
    ap.add_argument("dst_url", nargs="?", help=f"the target store, if {DST_URL_ENV} is not set")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of a line per table")
    args = ap.parse_args(argv)
    urls = copy_urls(args.src_url, args.dst_url, os.environ if environ is None else environ)
    if urls is None:
        print(f"error: name both stores, in {SRC_URL_ENV} and {DST_URL_ENV} or as SRC_URL DST_URL", file=sys.stderr)
        return 2
    src_url, dst_url = urls
    src, dst = _safe_url(src_url), _safe_url(dst_url)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        report, short = copy(src_url, dst_url, now), {}
    except CopyCheckFailed as failed:
        report, short = failed.report, failed.short
    except NotExactVersion as exc:  # names the role and the versions, never a URL
        print(f"error: could not copy {src} to {dst}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # the message may hold the URL; name only the class
        print(f"error: could not copy {src} to {dst}: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"from": src, "to": dst, "tables": report, "short": short, "ok": not short}, sort_keys=True))
    else:
        print(f"copy {src} -> {dst}")
        print("\n".join(_lines(report)))
        for n, c in short.items():
            print(f"FAIL {n}: destination has {c}, source has {report[n]['source']}")
    return 1 if short else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
