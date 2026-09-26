# Dashboards

This page shows how to run Apache Superset over your Coxswain runs, read
the Coxswain dashboard, and add a chart of your own. It assumes you have
never used Superset.

## What Superset is

Superset is a web app for charts and dashboards over SQL. You point it at
a data source, write a query, and save the result as a chart. Charts sit
together on a dashboard. Coxswain ships a Superset setup in
`deploy/superset` that reads your runs directory.

## What the dashboards show

The Coxswain dashboard answers four questions about your runs.

- **Cost.** Spend by day, by model, and by role.
- **Busy lanes.** How many lanes were busy in each hour.
- **Runs and quarantines.** How many runs happened and how many
  were quarantined.
- **Tool use.** Which tools were used, read from traces.

## Efficiency

The first rows of the dashboard ask whether the system is getting cheaper
and faster. Each chart answers one question.

- **Cost per turn by model, by day.** What one model turn costs, per model.
- **Cache-read share by model, by day.** The share of each model's input
  tokens read from cache that day. At a grain coarser than a day, the
  chart averages the daily shares weighted by turns, not by tokens.
- **Cost per landed task, by day.** The day's spend divided by the tasks
  that landed that day.
- **Landed tasks per day.** Throughput.
- **First-try build rate, by day.** The share of landed tasks that needed
  exactly one build call.
- **Lead time, launch to land (median hours), by day.** The median hours
  from a run's launch to its task landing.
- **Spend by task outcome, last 14 days.** Spend on tasks that landed, are
  still in flight, or never landed. A task counts as never landed once its
  last call is two days old and it has no landing in `runs/land.jsonl`.

A task landed when its record in `runs/land.jsonl` exited 0 and reached
`mark_done`. Land history starts when `runs/land.jsonl` began, which is
2026-09-25 in the reference workspace. Earlier days show cost but no
landings. The datasets that read that file are skipped, with their
charts, until both it and `runs/cox.db` exist.

The never-landed bar is not waste until the log covers the chart's whole
window. A task that landed before the log began has no landing record, so
it counts as never landed. In the reference workspace the bar overstates
waste until 2026-10-09, fourteen days after the log began.

## Start it

You need Docker with the compose plugin. From the repository root:

```sh
cd deploy/superset
cp .env.example .env
```

Open `.env` and replace every placeholder.

- `SUPERSET_SECRET_KEY` signs sessions. Generate a value with
  `openssl rand -base64 42`.
- `SUPERSET_ADMIN_PASSWORD` is the password for the admin user. Keep the
  other admin values or change them.
- `COXSWAIN_RUNS_DIR` is the path to your workspace's runs directory on
  the host.

Superset refuses to start when a variable is unset or still reads
`change-me`. Never commit `.env`.

```sh
docker compose up -d
```

The first start builds the image, so it takes a while. Then open
<http://127.0.0.1:8088>. The port is bound to your machine only.

## Log in and open the dashboard

Log in with `SUPERSET_ADMIN_USERNAME` and `SUPERSET_ADMIN_PASSWORD` from
your `.env`. The username is `admin` unless you changed it.

Open the Dashboards list from the top menu and choose the Coxswain
dashboard.

`docker compose up -d` also runs the `superset-dashboards` one-shot
service once Superset is healthy, and that service installs the
dashboards. It reads the specs in `deploy/superset/dashboards` and skips
the tool-use chart until graphs has written its first Parquet trace.
Run it again after a spec changes, or once that first trace exists. A
second run changes nothing when the specs already match Superset.

```sh
docker compose run --rm superset-dashboards
```

To confirm every chart returns data, export the values from your `.env`
and run the check from the repository root. It prints one line per chart,
`<chart>: <rows> rows` or `<chart>: ERROR <message>`, and exits 1 if any
chart errors.

```sh
python deploy/superset/check.py
```

## Change the time range

The dashboard has a time range filter. Open it, pick a range such as the
last week or a pair of dates, and apply it. Every chart on the dashboard
redraws for that range.

## Add a chart

Charts are built from a dataset, which is a saved SQL query or table.

1. Open SQL Lab from the top menu.
2. Choose the Coxswain database and write a query. Run it and check the
   rows.
3. Choose Save as chart. Superset saves the query as a dataset and opens
   the chart editor.
4. Pick a chart type, set its columns, and save the chart.
5. Add the chart to a dashboard from the save dialog.

## The data is read-only

`COXSWAIN_RUNS_DIR` is mounted into the container read-only at
`/data/runs`. Superset can query your runs but cannot change them.
Queries read the files each time they run, so a refresh shows new runs
with no import step.

Superset keeps its own users, datasets, and charts in a separate Docker
volume. Removing that volume loses your charts and never touches your
runs.

## Point the dashboard at Postgres

By default the dashboard reads the SQLite store at `/data/runs/cox.db`.
If your store lives in Postgres, set `COXSWAIN_STORE_URL` in `.env` to its
URL. The URL below is a placeholder.

```sh
COXSWAIN_STORE_URL=postgresql://reader:change-me@db.example:5432/coxswain
```

Use a read-only Postgres role. The connection is attached read-only, but
the role is the real limit. The store's tables must sit in the `public`
schema of the database the URL names. A table in another schema fails
when a chart queries it.

With the variable set, the datasets read the store through an attached
catalog named `store`. Their SQL reads `store.public.<table>`. With it
unset or empty, nothing changes. Default mode stays `sqlite_scan` of
`/data/runs/cox.db`.

The `superset-dashboards` one-shot writes that SQL into the datasets.
`docker compose up -d` runs it, as described above. If it did not run, the
datasets keep their old SQL and charts still read SQLite. Run it by hand
after changing the variable.

```sh
docker compose run --rm superset-dashboards
```

`runs/cox.db` must still exist in the runs directory in Postgres mode. The
one-shot decides which datasets to install by looking for files there, and
it never checks the store URL. Without `cox.db`, it skips every dataset
that reads the store, with its charts. It skips the landing datasets too.
The file only has to exist. The datasets still read Postgres.

The attached catalog was chosen over `postgres_scan`. A `postgres_scan` call
takes the connection string in its arguments. That puts the password in the
SQL of every dataset, and Superset saves that SQL. An attached catalog is
set up by the connection, not by the query. The dataset SQL names a table
in the catalog and never holds a URL.

A connect hook in `superset_config.py` does the attaching. Each time
Superset opens a DuckDB connection, the hook reads `COXSWAIN_STORE_URL` from
the container environment and runs ATTACH with it. So the password lives
only in that environment. The saved database object and the dataset SQL
hold no URL.

Only the store moves. Traces and `runs/land.jsonl` stay file-based. Superset
still reads them from the runs directory at `/data/runs`, even when the
store is in Postgres. A multi-machine setup therefore needs them on shared
storage later. [Several machines](several-machines.md) covers the
`traces_url` setting for traces. This page does not set up shared storage.

## Other data sources

The Superset image queries with DuckDB, so a dataset can read from more
than local files.

**A Postgres store.** See the section above, which sets it up without
putting the password in any SQL.

**Shared Parquet traces.** Point the dataset SQL at the shared location
with `read_parquet`. The path below is a placeholder.

```sql
SELECT *
FROM read_parquet('/data/runs/shared-traces/*.parquet')
```

A location outside `/data/runs` must be mounted into the container
first, and mounting it is a change to the deploy files.
