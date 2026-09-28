# The lake

This page shows how to sync your Coxswain runs into an Iceberg lake, keep
it fresh, and query it with SQL. It assumes you have never used Iceberg.

## Install it

The lake needs extra packages. Install them with the `lake` extra.

```sh
pip install 'coxswain-tools[lake]'
```

The extra brings pyiceberg with a SQL catalog, and duckdb.

## Sync your runs

`cox lake sync` appends the rows of each ended run to Iceberg tables. The
tables are runs, phases, node_calls, gate_decisions, ledger, and traces.

- **Once.** Each table has its own mark, so a run is appended once.
- **In place.** The Parquet trace files are registered where they sit.
  Nothing is copied.
- **Dry run.** `--dry-run` writes nothing.
- **Housekeeping.** The chair loop's housekeeping runs `cox lake sync` and
  a trace prune, both at the same `log_retention_days` age.

## Archive your logs

Each ended run also leaves a `<run>.log` and a `<run>.calls.jsonl`. `cox
lake sync` archives the ones older than `log_retention_days` days, a
routing-profile key that defaults to 7; a missing, invalid, or
non-positive value falls back to 7 too.

- **Where they go.** Archived logs land at `logs/<yyyy>/<mm>/<file>.gz`,
  beside the traces root: a traces root ending in `/traces` becomes
  `/logs`, so `s3://coxswain/traces` becomes `s3://coxswain/logs`. The
  year and month come from the file's last write, in UTC.
- **Verified before deleted.** The local file is deleted only after the
  archived copy reads back identical. A failed archive keeps the local
  file and is reported.
- **Reporting.** `cox lake sync` prints a logs line only when some logs
  were due. `--dry-run` reports what would be synced and writes nothing;
  `--json` prints the report as JSON.
- **Kept indefinitely.** The lake tables (runs, phases, node_calls,
  gate_decisions, ledger, traces) and the archived logs are kept
  indefinitely.

## Where it lives

Two values in your provider profile say where the lake is.

- **`lake_url`.** The warehouse, e.g. `s3://coxswain/lake`.
- **`lake_catalog_url`.** The Iceberg catalog. Any SQLAlchemy URL works,
  e.g. `postgresql+psycopg://user@host:5432/db`, which lets every machine
  that reaches the database find the tables.

With neither set, both default to files under the runs directory:
`lake-catalog.db` and `lake/`.

## Query it

`cox lake query "<sql>"` runs read-only DuckDB SQL over the lake tables.
Add `--json` to get the result as JSON.

## Check it

`cox lake doctor` checks the object store, the catalog, the namespace,
the warehouse, and each table, and exits 1 on a failed check. It creates
nothing.

## Keep it fresh

After the first sync, `cox runs land` syncs after each land.
