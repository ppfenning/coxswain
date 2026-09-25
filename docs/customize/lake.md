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
tables are runs, phases, node_calls, gate_decisions, and ledger.

- **Once.** Each table has its own mark, so a run is appended once.
- **In place.** The Parquet trace files are registered where they sit.
  Nothing is copied.
- **Dry run.** `--dry-run` writes nothing.

## Where it lives

Two values in your provider profile say where the lake is.

- **`lake_catalog_url`.** The catalog. It defaults to
  `runs/lake-catalog.db`.
- **`lake_url`.** The warehouse. It defaults to `runs/lake/`.

## Query it

`cox lake query "<sql>"` runs read-only DuckDB SQL over the lake tables.
Add `--json` to get the result as JSON.

## Check it

`cox lake doctor` checks the catalog, the warehouse, and each table. It
creates nothing.

## Keep it fresh

After the first sync, `cox runs land` syncs after each land.
