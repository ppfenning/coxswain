# Storage

This page describes where run data lives, which folder owns the schema, and how a chair moves historical records into the database by hand. Every table and column below is read from the migration modules `harness/store_ddl_0001.py` through `harness/store_ddl_0013.py`. A column those modules do not define is not documented here.

## The split

Three kinds of data live in three places.

Work items and intake stay markdown in git. The store mirrors their state in the `work_items` table, but the files remain the record a person edits.

Run records live in the database. That covers runs, phases, tasks, task records, attempts, model calls, the ledger, gate decisions, leases, the graphs registry, work item state and the claim queue, chair actions, the hosts table and the short-id counters.

Traces live outside git, under a trace root. The layout is `YYYY/MM/DD/<run_id>.parquet`, one zstd-compressed Parquet file per run, as the docstring of `harness/store_traces.py` states. Older day files, `YYYY/MM/DD/<run_id>.jsonl.zst`, still read when a run has no Parquet file. The root is the profile's `traces_url` (a path, `file://`, `s3://`, or a scheme a plugin serves; see `harness/traces_url.py`), else `<runs_dir>/traces`.

## Who owns what

The `src/graphs` folder owns the DDL, the numbered migrations, the `schema_version` table, the graphs registry and the dialect shim. The shim is `harness/store_dialect.py`. The migrations and their runner are in `harness/store_migrate.py` and the `harness/store_ddl_NNNN.py` modules.

The profile carries the connection string under the key `storage_url`. `_storage_url` in `harness/cli.py` returns it, and falls back to `cox.db` inside the runs directory when the key is absent. The accepted schemes are `sqlite://` and `postgresql://`.

Only `open_store` in `harness/store_migrate.py` applies migrations. The harness entry points that write (`shell.py`, and the `python -m harness.store_cli`, `store_queue`, `store_ids`, `store_copy` and `store_backfill` commands) open the store through it. The `cox` command in `src/tools` reads the store read-only and sends every write through one of those harness commands, so it never applies a migration itself.

`connect_readonly` in `harness/store_read.py` connects without writing and calls `check_version`. If the stored version differs from the version the code expects, it closes the connection and raises `StoreVersionError`. Its public read functions are `list_runs`, `run_summary`, `summary_row`, `cost_by_model`, `model_row`, `calls`, `attempts`, `ledger_rows`, `ledger_streak_rows`, `gate_decisions`, `current_lease`, `run_graph`, `task_record`, `task_records`, `build_task_history`, `work_items` and `read_trace`. All but `read_trace` take a connection. `read_trace` takes a trace root, a run id and a call id.

## Migrations

Each migration is one module and runs in its own transaction. `schema_version` gets one row per migration applied.

| Version | Module | What it does |
|---|---|---|
| 1 | `store_ddl_0001` | Creates `runs`, `phases`, `tasks`, `attempts`, `node_calls`, `ledger`, `gate_decisions` and `leases`, and four indexes. |
| 2 | `store_ddl_0002` | Creates the graphs registry (`graphs`, `graph_nodes`, `graph_edges`) and index `ix_graphs_name_version`; adds `runs.graph_id` and `node_calls.node_id`. |
| 3 | `store_ddl_0003` | Adds `runs.host`, the host the run was launched from. |
| 4 | `store_ddl_0004` | Creates `task_records`. |
| 5 | `store_ddl_0005` | Adds `attempts.cause` and `attempts.cause_why`. |
| 6 | `store_ddl_0006` | Creates `work_items`. |
| 7 | `store_ddl_0007` | Creates `chair_actions`. |
| 8 | `store_ddl_0008` | Creates `hosts`. |
| 9 | `store_ddl_0009` | Adds the queue and claim columns to `work_items`. |
| 10 | `store_ddl_0010` | Adds `runs.paused_at` and `leases.status`. |
| 11 | `store_ddl_0011` | Creates `id_sequence`; adds `work_items.short_id` and `runs.short_id`. |
| 12 | `store_ddl_0012` | Adds `work_items.priority`. |
| 13 | `store_ddl_0013` | Adds `work_items.issue_number`, `work_items.project_item_id` and `work_items.parent_item`. |

Before `open_store` applies a pending migration it writes a backup: a SQLite copy, or `pg_dump -Fc` for Postgres, named `store-v<current>-to-v<newest>-<timestamp>.sqlite` or `.dump`. The backups go to `$COX_STORE_BACKUP_DIR`, else `~/.local/state/coxswain/backups`. A Postgres store needs `pg_dump` on the PATH. `COX_STORE_MIGRATE_WITHOUT_BACKUP=1` skips the backup and prints a warning.

To see what a store still needs without applying anything:

```
python -m harness.store_migrate --pending STORE_URL
```

It prints one `NNNN description` line per pending migration, or `up to date (vN)`. It exits 2 when the store is newer than the code.

## Table reference

Timestamps are ISO-8601 TEXT. Booleans are SMALLINT holding 0 or 1. JSON columns are TEXT on sqlite and JSONB on Postgres, and `connect` hands both back as text. Every table has a primary key. Columns added by a later migration are nullable unless the migration gives a default.

### schema_version

Created by `harness/store_migrate.py`, not by a DDL module. Columns: `version` INTEGER, the primary key; `applied_at` TEXT; `description` TEXT.

### runs

Primary key `run_id`. TEXT columns: `run_id`, `principal`, `launched_by`, `launched_at`, `cartridge_sha`, `cartridge_team`, `overlay_sha`, `provider_profile`, `started_at`, `ended_at`, `status`. JSON column: `record_json`.

Added later, all nullable TEXT: `graph_id` (0002), `host` (0003), `paused_at` (0010; a run is paused exactly when it is not null), `short_id` (0011).

### phases

Primary key `run_id`, `phase_id`. TEXT columns: `run_id`, `phase_id`, `ts`, `principal`. REAL column: `human_minutes`. JSON columns: `totals_json`, `record_json`.

### tasks

Primary key `run_id`, `task_id`. TEXT columns: `run_id`, `phase_id`, `task_id`, `state`, `updated_at`.

### task_records

Added by migration 0004. One row per task of a phase of a run. Primary key `run_id`, `phase_id`, `task_id`. TEXT columns: `run_id`, `phase_id`, `task_id`, `updated_at`. JSON column: `record_json`, the task record.

### attempts

Primary key `run_id`, `task_id`, `seq`. TEXT columns: `run_id`, `task_id`, `phase_id`, `kind`, `reason`, `ts`. INTEGER column: `seq`. Index `ix_attempts_task` on `task_id`.

Added by migration 0005, nullable TEXT: `cause` and `cause_why`.

### node_calls

Primary key `call_id`.

- TEXT: `call_id`, `run_id`, `phase_id`, `task_id`, `role`, `tier`, `model_alias`, `model_id`, `claude_code_version`, `ceiling_source`, `ts`, `requested_tier`, `chosen_tier`, `decision_reason`, `router_tier`, `router_reason`, `ticket_key`, `outcome_key`, `system_one_prediction`.
- INTEGER: `seq`, `turns`, `duration_ms`, `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `input_total`, `output_tokens`.
- REAL: `cost_usd`, `ceiling_usd`, `system_one_confidence`.
- Boolean: `ok`.
- JSON: `decision_json`, `detail_json`.
- Added by migration 0002: `node_id`, a nullable TEXT column.

Indexes: `ix_node_calls_run` on `run_id`, and `ix_node_calls_role_tier` on `role`, `tier`.

### ledger

Primary key `row_hash`. TEXT columns: `row_hash`, `run_id`, `ts`, `principal`, `kind`, `risk`, `outcome`, `cartridge_sha`, `provider_profile`, `schema_tag`. BIGINT column: `epoch`. JSON column: `row_json`. Index `ix_ledger_run` on `run_id`.

### gate_decisions

Primary key `run_id`, `phase_id`, `seq`. TEXT columns: `run_id`, `phase_id`, `kind`, `target`, `decision`, `risk`, `outcome`. INTEGER column: `seq`. Boolean columns: `applied`, `edited`. BIGINT column: `epoch`. JSON column: `detail_json`.

### leases

Primary key `name`. TEXT columns: `name`, `holder`, `heartbeat_at`, `expires_at`. BIGINT column: `epoch`.

Added by migration 0010: `status`, nullable TEXT, what the lease's last heartbeat was doing (for example `paused`).

### graphs

Added by migration 0002. Primary key `graph_id`. TEXT columns: `graph_id`, `name`, `version`, `content_hash`, `registered_at`. JSON column: `definition_json`. Index `ix_graphs_name_version` on `name`, `version`. The `graph_id` equals the `content_hash`. `version` is TEXT, so a writer must write it as text.

### graph_nodes

Added by migration 0002. Primary key `graph_id`, `node_id`. TEXT columns: `graph_id`, `node_id`, `role`, `default_tier`, `default_class`, `output_schema_hash`. INTEGER column: `ord`.

### graph_edges

Added by migration 0002. Primary key `graph_id`, `src`, `dst`. All three are TEXT.

### work_items

Added by migration 0006. One row per task of an initiative. Primary key `initiative`, `task_id`. The 0006 columns are all NOT NULL: TEXT `initiative`, `task_id`, `phase`, `state`, `updated_at`, `updated_by`; JSON `needs_json`.

Migration 0009 adds the queue and claim columns:

- Queue: TEXT `kind`, `title`, `body`; JSON `surfaces_json`, `extra_json`. All nullable.
- Claim: TEXT `holder`, `expires_at`, nullable; INTEGER `epoch`, default 0, so an unclaimed row reads epoch 0.

Later migrations add:

- `short_id` (0011), nullable TEXT.
- `priority` (0012), INTEGER NOT NULL DEFAULT 0, higher first.
- `issue_number` INTEGER, `project_item_id` TEXT and `parent_item` TEXT (0013), all nullable: the keys of the work item's card on a tracker.

### chair_actions

Added by migration 0007. One row per action the chair takes. Primary key `ts`, `holder`, `kind`, `target`. Every column is NOT NULL. TEXT columns: `ts`, `holder`, `kind`, `target`, `status`, `reason`. INTEGER column: `epoch`. JSON column: `action_json`, the whole action line as the chair wrote it. `target` is the action's task id, else its initiative, else its first intake id, else the empty string.

### hosts

Added by migration 0008. One row per machine the chair may dispatch to. Primary key `name`. Every column is NOT NULL. TEXT columns: `name`, `ssh`, `state`, `beat_at`, `updated_at`, `updated_by`. INTEGER column: `capacity`. JSON column: `versions_json`. `state` is `active`, `draining` or `offline`. `beat_at` is the empty string until the first beat. `versions_json` holds what the host reported: its cox, graphs, cartridges and claude versions, and `login_ok`.

### id_sequence

Added by migration 0011. One row per scope: the next short id to allocate from that scope. Primary key `scope`. Columns, both NOT NULL: `scope` TEXT, `next` INTEGER.

### Joins are by value

No table carries a foreign-key clause. `runs.graph_id` and `node_calls.node_id` cannot, because SQLite cannot add a constraint by ALTER TABLE, and every migration since 0002 follows the same rule. A join to `graphs`, `graph_nodes`, `runs`, `phases` or `work_items` is by value, and nothing in the database enforces it.

## Adding a migration

A migration is a module in the `harness` package named `store_ddl_NNNN`, four digits, contiguous from 0001. It exposes `VERSION`, `DESCRIPTION` and `statements(dialect)`. The number in the name and `VERSION` must agree, or the runner raises `MigrationError`. A gap or a duplicate number raises the same error.

Adding one never edits `harness/store_migrate.py`, because `default_modules` finds the modules with `pkgutil`. The newest is 0013, so the next is `harness/store_ddl_0014.py` with `VERSION = 14`.

Test it two ways. From an empty database, it must reach the new version. From a database at the previous version, it must apply only the new migration. `tests/test_store_ddl_0002.py` does both and is the pattern to copy: see `test_an_empty_database_reaches_version_two` and `test_a_database_at_version_one_applies_only_0002`.

A migration that adds columns to an existing table also lists them in a module-level `_ADDED` tuple of `(table, column)` pairs. `harness/store_copy.py` reads that shape when it copies a store from one URL to another.

## Portability rules

The store runs on sqlite and on Postgres, so the SQL in it stays portable.

- Timestamps are ISO-8601 TEXT. Booleans are SMALLINT 0 or 1. JSON columns use `dialect.json_type`.
- Every table has a primary key, so a writer can use `insert_ignore` or `upsert` from `harness/store_dialect.py`.
- SQL takes its placeholder from `dialect.placeholder`, which is `?` on sqlite and `%s` on Postgres.
- A column added by ALTER TABLE carries no foreign-key clause, because SQLite cannot add a constraint that way. The docstring of `harness/store_ddl_0002.py` explains.

`forbidden_constructs` in `harness/store_dialect.py` returns the banned tokens found in a piece of SQL. The banned tokens are `AUTOINCREMENT`, `PRAGMA`, `INSERT OR REPLACE` (which also catches `REPLACE INTO`), `strftime` and `json_extract`. The last two count only as calls, so a column named `strftime` passes. Comments and quoted text are blanked before matching.

## The lease and the fencing epoch

`harness/store_lease.py` keeps a leader lease as one row per `name` in `leases`, fenced by a monotonic `epoch`. Time is always an argument: an ISO timestamp string and a ttl in seconds. Nothing in the module reads the clock. Every state change is one conditional statement whose affected row count decides the outcome, so two writers cannot both win.

- `acquire(conn, name, holder, now, ttl, steal=False)` takes a free or expired lease and increments `epoch`. The current holder acquiring again also increments it. With `steal`, a lease another live holder has is replaced at epoch + 1, conditioned on the epoch just read, so two stealers cannot both win. A refusal is a `LeaseResult` with `ok` False and the current holder.
- `renew(conn, name, holder, epoch, now, ttl)` extends a live lease. An expired or released lease is not revived, and the holder must acquire again.
- `release(conn, name, holder, epoch)` expires the lease in the past. The row and its epoch stay, so the next holder still increments.
- `assert_epoch(conn, name, epoch, now)` is the fence. It is true only when `epoch` is the stored one and the lease is unexpired at `now`.
- `lease_state(row, now)` returns `free`, `expired` or `held`.

Every leader write calls `assert_epoch` immediately before it writes, and carries the epoch. The `ledger` and `gate_decisions` tables each have an `epoch` column.

## Querying traces and joining to the database

A trace row has the columns `run_id`, `call_id`, `seq`, `day`, `type`, `subtype`, `tool` and `event`, as `harness/trace_columns.py` defines them. `event` is the raw event as a JSON string. The `call_id` is the same value as `node_calls.call_id`.

The DuckDB query below reads the whole trace tree with one glob and joins each row to its call. It needs DuckDB's sqlite extension. The paths are placeholders for your trace root and your database file. The query is not run by any test. Run it once on your DuckDB version before relying on it.

```sql
INSTALL sqlite;
LOAD sqlite;
ATTACH '/data/cox.db' AS cox (TYPE sqlite, READ_ONLY);

SELECT c.role, c.tier, t.type, count(*) AS events
FROM read_parquet('/data/traces/*/*/*/*.parquet') AS t
JOIN cox.node_calls AS c ON c.call_id = t.call_id
GROUP BY c.role, c.tier, t.type
ORDER BY events DESC;
```

The same read from Python is `read_trace(root, run_id, call_id)` in `harness/store_read.py`, which calls `read_call` in `harness/store_traces.py`. To print one run's rows as JSON lines, run `python -m harness.store_traces dump TRACES_ROOT RUN_ID`. Reading or writing Parquet traces needs `pyarrow` (the `parquet` or `traces` extra). Reading legacy `.jsonl.zst` files also needs `zstandard` (the `traces` extra).

## Postgres

Stay on sqlite while one machine writes the run records. Move to Postgres when records are written from more than one machine. The tables are the same. The dialect shim renders the JSON type as JSONB and the placeholder as `%s`.

A `postgresql://` URL needs the `postgres` extra in `pyproject.toml`. Without the driver, `connect` raises `StoreDriverMissing`.

The suite does not run against a Postgres server; it covers the rendered SQL. The comment on `_has_version_table` in `harness/store_migrate.py` reads "unknown: the postgres branch is not run in this repository's tests". Run `python -m harness.store_migrate --pending` against a new server, and check the result, before pointing lanes at it.

## Chair runbook: backfill by hand

This moves historical files into the store. Run the two commands in this order. Every path is an argument.

### 1. Run records

```
python -m harness.store_backfill RUNS_DIR WORK_DIR LEDGER STORE_URL
```

- `RUNS_DIR` is the directory of run, phase, launch, call and usage files.
- `WORK_DIR` is the directory of task markdown files.
- `LEDGER` is the ledger file, one JSON object per line.
- `STORE_URL` is `sqlite:///<absolute path>` or `postgresql://...`. The command calls `open_store`, so it applies migrations.

It prints a JSON report and exits 0 only when every table balances. The report has one entry per table: `runs`, `phases`, `gate_decisions`, `node_calls`, `ledger` and `attempts`. Each entry has four counts: `seen`, `inserted`, `already_present` and `malformed`. A table balances when `seen` minus `malformed` equals `inserted` plus `already_present`. Exit 1 means at least one table does not balance.

### 2. Traces

```
python -m harness.store_backfill_traces TRACES_ROOT [--loose-root DIR] [--calls-dir DIR]
```

- `TRACES_ROOT` is the local trace store root. Parquet files are written here, and sources are archived under `TRACES_ROOT/archive/`.
- `--loose-root DIR` is the directory of `<run_id>-trace` directories, one `<role>-<n>.jsonl` file per call.
- `--calls-dir DIR` is the directory of `<run_id>.calls.jsonl` files, which give each call its day and call id.

The command repacks every source of a run, loose files and legacy day files under `TRACES_ROOT`, into one Parquet file per run. It moves a source into `archive/` only after the Parquet file reads back with the same event count for every call. It never deletes. A trace with no matching call is imported as `<run_id>-<role>-<n>`, dated by file modification time.

It prints one `name: value` line per field: `runs`, `sources`, `empty`, `rows`, `archived`, `blocked`, `src_bytes`, `dst_bytes` and `unavailable`, then one line per mismatch or conflict. It exits 1 on any mismatch, conflict or blocked run, or when a dependency is missing, and 2 on bad arguments.

Two follow-up subcommands:

- `python -m harness.store_backfill_traces relink TRACES_ROOT [--store-url URL] [--dry-run]` rewrites the synthetic `<run_id>-<role>-<n>` call ids to the `legacy:` ids the store holds for the same calls, so traces and `node_calls` join.
- `python -m harness.store_backfill_traces prune TRACES_ROOT [--dry-run] [--older-than DAYS] [--calls-dir DIR] [--store-url URL]` deletes archived sources whose Parquet file holds every event, and nothing else.

### The archive rule

Files are archived only after the counts balance and match the stats database. Balanced counts show that the importer wrote what it read. They do not show that it read everything, so compare the per-table counts in the reports against the stats database before archiving anything.

`archive_imported` in `harness/store_backfill.py` refuses and moves nothing unless the report balances, and it never deletes. It is a function, not a flag of the command. The comparison with the stats database is a chair step, and neither module does it.

The trace repack archives in the same run, but only per run and only after the read-back matches. Run `prune` only after you have compared the counts.
