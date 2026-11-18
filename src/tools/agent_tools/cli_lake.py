"""`cox lake`: the lake sync, query and doctor commands."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import types
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_lazy import chair_read_exits, lake_config, lake_lease, route, run_logs, store_url


def _lake_provider(a: argparse.Namespace) -> tuple[Mapping, str | None]:
    """Edge. (provider profile, problem). A profile that is named but unusable is a problem, not a silent default lake.

    Only an unnamed profile falls back to files in the runs dir."""
    explicit = a.profile or os.environ.get("AGENT_TOOLS_PROFILE")
    text = _read_text_or_none(_profile_path(a))
    if text is None:
        return {}, f"no profile at {_profile_path(a)}" if explicit else None
    try:
        routing_profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return {}, f"profile unreadable: {exc}"
    provider_path = routing_profile.get("provider_profile")
    if not provider_path:
        return {}, None
    provider_text = _read_text_or_none(Path(provider_path).expanduser())
    if provider_text is None:
        return {}, f"provider profile not readable: {provider_path}"
    provider = store_url.read_provider_profile(provider_path)
    has_content = any(line.strip() and not line.lstrip().startswith("#") for line in provider_text.splitlines())
    if has_content and not provider:
        return {}, f"provider profile is not a YAML mapping: {provider_path}"
    return provider, None


def _lake_sync_report(results: Sequence, traces: tuple[int, int, int], dry_run: bool, new_tables: Sequence[str],
                      catalog: str, warehouse: str, traces_root: str) -> dict:
    """`results` are `SyncResult`s and `traces` is (found, registered, skipped). Every URL is redacted."""
    found, registered, skipped = traces
    return {
        "dry_run": dry_run,
        "new_tables": list(new_tables),
        "catalog": lake_config.redact(catalog),
        "warehouse": lake_config.redact(warehouse),
        "traces_root": lake_config.redact(traces_root),
        "tables": [
            {"table": r.table, "rows_found": r.rows_found, "rows_appended": r.rows_appended, "mark": r.new_mark}
            for r in results
        ],
        "traces": {"found": found, "registered": registered, "skipped": skipped, "would_register": found - skipped},
    }


def _lake_sync_lines(report: dict) -> list[str]:
    """A dry run says what a real run would do; its mark is the one it would set."""
    dry = report["dry_run"]
    created = [f"lake: would create {', '.join(report['new_tables'])}"] if report["new_tables"] else []
    tables = [
        f"{t['table']}: would append {t['rows_found']} (mark {t['mark'] or 'none'})" if dry
        else f"{t['table']}: {t['rows_appended']} appended (mark {t['mark'] or 'none'})"
        for t in report["tables"]
    ]
    traces = report["traces"]
    trace_line = (
        f"traces: would register {traces['would_register']}, {traces['skipped']} skipped" if dry
        else f"traces: {traces['registered']} registered, {traces['skipped']} skipped"
    )
    logs = report.get("logs")
    log_line = [] if logs is None or not logs["due"] else [
        f"logs: would archive {logs['due']} past {logs['retention_days']} days" if dry
        else f"logs: {logs['archived']} of {logs['due']} archived past {logs['retention_days']} days"
        + (f", {len(logs['failed'])} failed: {logs['failed'][0]}" if logs["failed"] else "")
    ]
    return [*created, *tables, trace_line, *log_line]


def _lake_catalog_missing(catalog_uri: str) -> bool:
    """True for a `sqlite:///` catalog whose file does not exist yet; any other catalog is assumed to exist."""
    return catalog_uri.startswith("sqlite:///") and not Path(catalog_uri.removeprefix("sqlite:///")).exists()


def _lake_real_run(config: lake_config.LakeConfig, store: str, root: str,
                   runs_dir: Path | None = None, retention: int | None = None) -> Callable[[], dict]:
    """Edge. Create any missing table; return the callable a lease guards that appends the new rows, registers
    traces, and archives ended runs' logs past `retention` days beside the traces before removing the local copies."""
    retention = run_logs.DEFAULT_RETENTION_DAYS if retention is None else retention
    catalog = lake_config.load_catalog(config)
    # Imported here: these modules import pyiceberg and pyarrow at module top, and cli.py must load without the extra.
    from agent_tools import lake_sync, lake_tables, lake_traces

    lake_tables.ensure_tables(catalog)

    def sync() -> dict:
        results = lake_sync.sync(catalog, store)
        traces = lake_traces.register_traces(catalog, root)
        report = _lake_sync_report(results, traces, False, (), config.catalog_uri, config.warehouse, root)
        if runs_dir is not None:
            report["logs"] = _run_logs_report(_archive_run_logs(config, root, runs_dir, retention, dry_run=False), retention)
        return report

    return sync


def _archive_run_logs(config: lake_config.LakeConfig, root: str, runs_dir: Path, retention: int,
                      dry_run: bool) -> run_logs.Outcome:
    """Edge. Ended runs' logs past retention, archived beside the traces root through the lake's own object-store settings."""
    from pyiceberg.io import load_file_io

    target = run_logs.logs_root(root)
    io = load_file_io(lake_config.iceberg_properties(config.object_store, os.environ), target)
    ended = {str(row.get("run_id") or "") for row in chair_read_exits.exit_rows(runs_dir) if chair_read_exits.row_exited(row)}
    now = datetime.datetime.now(datetime.UTC)
    return run_logs.archive_and_prune(runs_dir, target, io, ended, now, retention, dry_run=dry_run)


def _run_logs_report(outcome: run_logs.Outcome, retention: int) -> dict:
    return {"retention_days": retention, "due": outcome.due, "archived": outcome.archived, "failed": outcome.failed}


def _lake_open_readonly(config: lake_config.LakeConfig, name: str):
    """Edge. The configured catalog opened so that it creates nothing; None when it holds no catalog tables yet."""
    from pyiceberg.catalog.sql import IcebergTables, SqlCatalog
    from sqlalchemy import inspect as sql_inspect

    # Measured: a SqlCatalog creates its SQLite file on first connect, and its catalog tables on construction on any backend.
    if _lake_catalog_missing(config.catalog_uri):
        return None
    catalog = SqlCatalog(name, uri=config.catalog_uri, warehouse=config.warehouse, init_catalog_tables="false")
    return catalog if sql_inspect(catalog.engine).has_table(IcebergTables.__tablename__) else None


def _lake_dry_run(config: lake_config.LakeConfig, store: str, root: str) -> dict:
    """Edge. What a real run would do, read from the lake without creating or writing anything.

    A table the lake lacks reads as empty: no mark, no data files."""
    # An in-memory catalog: it raises LakeUnavailable when the extra is missing, names the catalog, and touches no file.
    probe = lake_config.load_catalog(lake_config.LakeConfig("sqlite:///:memory:", config.warehouse))
    from pyiceberg.exceptions import NoSuchTableError
    from pyiceberg.io import load_file_io

    from agent_tools import lake_sync, lake_tables, lake_traces

    real = _lake_open_readonly(config, probe.name)
    names = [f"{lake_tables.NAMESPACE}.{name}" for name in lake_tables.TABLES]
    new_tables = names if real is None else [n for n in names if not real.table_exists(n)]
    # The three attributes a dry run reads from a table; tests pin that lake_sync and lake_traces read no others.
    absent = types.SimpleNamespace(
        properties={}, io=load_file_io(dict(probe.properties), root),
        scan=lambda: types.SimpleNamespace(plan_files=lambda: []),
    )

    def load_table(identifier: str):
        try:
            return absent if real is None else real.load_table(identifier)
        except NoSuchTableError:
            return absent

    # The one catalog method a dry run calls; tests pin that lake_sync and lake_traces call no other.
    preview = types.SimpleNamespace(load_table=load_table)
    results = lake_sync.sync(preview, store, dry_run=True)
    traces = lake_traces.register_traces(preview, root, dry_run=True)
    return _lake_sync_report(results, traces, True, new_tables, config.catalog_uri, config.warehouse, root)


def _lake_refuse(a: argparse.Namespace, message: str) -> int:
    print(json.dumps({"error": message}) if a.json else message)
    return 2


def _lake_sync_done(a: argparse.Namespace, report: dict) -> int:
    print(json.dumps(report, indent=2) if a.json else "\n".join(_lake_sync_lines(report)))
    return 0


def _lake_sync_outcome(a: argparse.Namespace, outcome: lake_lease.SyncOutcome) -> int:
    """Report a real run's outcome once the lease decided whether it ran; a held or errored sync still exits 0."""
    if isinstance(outcome, lake_lease.Synced):
        return _lake_sync_done(a, outcome.value)
    if isinstance(outcome, lake_lease.SkippedHeld):
        message = f"lake: sync skipped (held by {outcome.holder})"
        print(json.dumps({"status": "held", "holder": outcome.holder}, indent=2) if a.json else message)
        return 0
    message = f"lake: sync skipped ({outcome.name})"
    print(json.dumps({"status": outcome.name}, indent=2) if a.json else message)
    return 0


def _log_retention_days(a: argparse.Namespace) -> int:
    """Edge. The routing profile's log_retention_days; the default when the profile is missing or unreadable."""
    try:
        return run_logs.retention_days(route.parse_profile(_read_text_or_none(_profile_path(a)) or ""))
    except route.ProfileError:
        return run_logs.DEFAULT_RETENTION_DAYS


def _lake_sync(a: argparse.Namespace) -> int:
    """Edge. Sync the run store and the trace files into the Iceberg lake; `--dry-run` creates and writes nothing."""
    # Absolute: `file://runs/lake` would name a host, and a relative trace path registered once would break from another cwd.
    runs_dir = Path(a.runs_dir).resolve()
    provider, problem = _lake_provider(a)
    if problem:
        return _lake_refuse(a, f"lake: {problem}")
    config = lake_config.resolve_lake(provider, runs_dir)
    store = store_url.profile_store_url(provider, runs_dir)
    root = store_url.profile_traces_root(provider, runs_dir).url
    root = root if "://" in root else str(Path(root).resolve())
    try:
        if a.dry_run:
            return _lake_sync_done(a, _lake_dry_run(config, store, root))
        retention = _log_retention_days(a)
        outcome = lake_lease.sync_under_lease(_lake_real_run(config, store, root, runs_dir, retention), runs_dir)
    except lake_config.LakeUnavailable as err:
        return _lake_refuse(a, str(err))
    except ImportError as err:
        # pyiceberg imported, but a module the lake also needs, such as pyarrow, did not.
        return _lake_refuse(a, f"the Iceberg lake needs the optional extra `lake`; {err.name or err} is missing: "
                               "pip install 'coxswain[lake]'")
    return _lake_sync_outcome(a, outcome)


def _lake_config_for(a: argparse.Namespace) -> tuple[lake_config.LakeConfig | None, str | None]:
    """Edge. (lake config, problem) for a read command; the same resolution `cox lake sync` uses."""
    provider, problem = _lake_provider(a)
    if problem:
        return None, f"lake: {problem}"
    return lake_config.resolve_lake(provider, Path(a.runs_dir).resolve()), None


def _lake_extra_missing(a: argparse.Namespace, err: ImportError) -> int:
    # pyiceberg or a module the lake also needs, such as duckdb or pyarrow, did not import.
    return _lake_refuse(a, f"the Iceberg lake needs the optional extra `lake`; {err.name or err} is missing: "
                           "pip install 'coxswain[lake]'")


def _lake_query(a: argparse.Namespace) -> int:
    """Edge. Run DuckDB SQL over the lake tables and print the rows as a table, or as JSON with `--json`."""
    config, problem = _lake_config_for(a)
    if config is None:
        return _lake_refuse(a, problem or "lake: no config")
    try:
        import duckdb

        from agent_tools import lake_query

        columns, rows = lake_query.query(lake_config.load_catalog(config), a.sql)
    except lake_config.LakeUnavailable as err:
        return _lake_refuse(a, str(err))
    except ImportError as err:
        return _lake_extra_missing(a, err)
    except duckdb.Error as err:
        return _lake_refuse(a, str(err))
    print(json.dumps(lake_query.rows_to_json(columns, rows), indent=2) if a.json else lake_query.render_table(columns, rows))
    return 0


def _lake_doctor(a: argparse.Namespace) -> int:
    """Edge. One line per lake check; exit 1 when the verdict is fail."""
    config, problem = _lake_config_for(a)
    if config is None:
        return _lake_refuse(a, problem or "lake: no config")
    try:
        from agent_tools import lake_doctor
    except ImportError as err:
        return _lake_extra_missing(a, err)
    checks = lake_doctor.run_checks(config)
    verdict = lake_doctor.verdict(checks)
    if a.json:
        print(json.dumps({"verdict": verdict, "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks]}, indent=2))
    else:
        print("\n".join([*(f"{c.status} {c.name}: {c.detail}" for c in checks), f"verdict: {verdict}"]))
    return 1 if verdict == lake_doctor.FAIL else 0
