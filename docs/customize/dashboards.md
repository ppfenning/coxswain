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

## Fleet

The Fleet dashboard answers five questions about the machines running
Coxswain, one chart per panel.

- **Chair holder, epoch and beat age.** Who holds the chair lease, which
  host and epoch it holds it at, and how many seconds have passed since
  its last heartbeat.
- **Lanes per host over time against capacity.** How many lanes each
  host ran per hour over the last 14 days, next to the max-in-flight
  limit in force at that hour.
- **Host state and last login.** Every host's state and when it last
  checked in.
- **Needs-chair items by cause.** How many tasks are waiting on the
  chair, grouped by why, and how long the oldest one in each group has
  waited.
- **Weekly spend against the ceiling.** Cumulative spend since the most
  recent weekly reset, next to the weekly_fraction the chair loop
  recorded at each point.

There is no drafts panel yet. The store keeps no draft or approval state
on `task_records`, so a dataset that lists items waiting on a draft has
no column to filter on; see the `fleet_drafts_waiting_approval` comment
in `deploy/superset/dashboards/datasets.yaml`.

Fleet reads the store only: `hosts`, `chair_actions`, `chair_ticks`,
`leases`, `runs`, and `node_calls`. It reads them through the same
connection as the Coxswain and Chair dashboards, `sqlite_scan` of
`cox.db` or an attached Postgres catalog, and never
`chair.actions.jsonl`, `land.jsonl`, or any other log file. None of its
datasets reads `task_records`.

The host-state panel is skipped until the `hosts` table exists in the
store. `fleet_host_state` requires `hosts-table`, and bootstrap installs
a dataset only once its required source is present. Once a run has
written to `hosts`, rerun `docker compose run --rm superset-dashboards`
to install the panel.

bootstrap.py creates every Fleet chart from the specs in
`deploy/superset/dashboards/charts.yaml`, but it still groups only the
Coxswain charts into a Superset dashboard page. The comment atop
`deploy/superset/dashboards/dashboard.yaml` names teaching it to also
build the Fleet and Chair pages as a separate, later change. Until then,
open Charts from the top menu and find a panel by its title; a chart
opened this way shows the same query a dashboard page would.

Four of the five panels read a dataset new to Fleet. Their SQL, copied
from `deploy/superset/dashboards/datasets.yaml`:

`chair_lanes_by_host_hour` feeds **Lanes per host over time against
capacity**:

```sql
WITH hours AS (
  SELECT h AS hour_start, h + INTERVAL 1 HOUR AS hour_end
  FROM generate_series(
    date_trunc('hour', CAST(now() AT TIME ZONE 'UTC' AS TIMESTAMP)) - INTERVAL 14 DAY,
    date_trunc('hour', CAST(now() AT TIME ZONE 'UTC' AS TIMESTAMP)),
    INTERVAL 1 HOUR
  ) AS t(h)
),
r AS (
  SELECT CAST(launched_at AS TIMESTAMP) AS launched_at, CAST(ended_at AS TIMESTAMP) AS ended_at,
         COALESCE(NULLIF(host, ''), 'local') AS host
  FROM {{store:runs}}
),
ticks AS (
  SELECT CAST(ts AS TIMESTAMP) AS ts, max_in_flight FROM {{store:chair_ticks}}
),
busy AS (
  SELECT hours.hour_start, r.host, COUNT(*) AS lanes
  FROM hours
  JOIN r ON r.launched_at <= hours.hour_end
        AND COALESCE(r.ended_at, CAST(now() AT TIME ZONE 'UTC' AS TIMESTAMP)) >= hours.hour_start
  GROUP BY ALL
)
SELECT busy.hour_start AS hour, busy.host, busy.lanes,
       (SELECT t.max_in_flight FROM ticks AS t WHERE t.ts <= busy.hour_start ORDER BY t.ts DESC LIMIT 1) AS max_in_flight
FROM busy
ORDER BY hour, host
```

`fleet_host_state` feeds **Host state and last login**:

```sql
SELECT host, state, CAST(last_login_check_at AS TIMESTAMP) AS last_login_check_at
FROM {{store:hosts}}
```

`fleet_needs_chair_by_cause` feeds **Needs-chair items by cause**:

```sql
WITH flagged AS (
  SELECT initiative, task, reason, CAST(ts AS TIMESTAMP) AS flagged_at,
         ROW_NUMBER() OVER (PARTITION BY initiative, task ORDER BY CAST(ts AS TIMESTAMP) DESC) AS rn
  FROM {{store:chair_actions}} WHERE kind = 'needs_chair'
),
landed AS (
  SELECT initiative, task, CAST(ts AS TIMESTAMP) AS landed_at
  FROM {{store:chair_actions}} WHERE kind = 'land' AND status = 'ok'
),
waiting AS (
  SELECT f.reason, f.flagged_at
  FROM flagged AS f
  WHERE f.rn = 1
    AND NOT EXISTS (
      SELECT 1 FROM landed AS l WHERE l.initiative = f.initiative AND l.task = f.task AND l.landed_at > f.flagged_at
    )
)
SELECT reason AS cause, COUNT(*) AS items,
       MAX(date_diff('minute', flagged_at, CAST(now() AT TIME ZONE 'UTC' AS TIMESTAMP))) / 60.0 AS oldest_waiting_hours
FROM waiting
GROUP BY reason
```

`fleet_weekly_spend` feeds **Weekly spend against the ceiling**:

```sql
WITH ticks AS (
  SELECT CAST(ts AS TIMESTAMP) AS ts, weekly_fraction,
         LAG(weekly_fraction) OVER (ORDER BY CAST(ts AS TIMESTAMP)) AS prior_fraction
  FROM {{store:chair_ticks}}
),
reset_at AS (
  SELECT COALESCE(
    MAX(CASE WHEN prior_fraction IS NOT NULL AND weekly_fraction < prior_fraction THEN ts END),
    MIN(ts)
  ) AS reset_ts
  FROM ticks
),
cost AS (
  SELECT CAST(CAST(ts AS TIMESTAMP) AS DATE) AS day, SUM(cost_usd) AS cost_usd
  FROM {{store:node_calls}} GROUP BY 1
),
tick_days AS (
  SELECT DISTINCT CAST(ts AS DATE) AS day FROM ticks
),
days AS (
  SELECT day FROM cost WHERE day >= (SELECT CAST(reset_ts AS DATE) FROM reset_at)
  UNION
  SELECT day FROM tick_days WHERE day >= (SELECT CAST(reset_ts AS DATE) FROM reset_at)
)
SELECT days.day, COALESCE(cost.cost_usd, 0.0) AS cost_usd,
       SUM(COALESCE(cost.cost_usd, 0.0)) OVER (ORDER BY days.day) AS cumulative_cost_usd,
       (SELECT t.weekly_fraction FROM ticks AS t WHERE CAST(t.ts AS DATE) <= days.day ORDER BY t.ts DESC LIMIT 1) AS weekly_fraction
FROM days
LEFT JOIN cost ON cost.day = days.day
ORDER BY days.day
```

The fifth panel, **Chair holder, epoch and beat age**, reads the
`chair_status` dataset.

## Chair

The Chair dashboard answers three questions about the always-on chair
loop, one chart per panel.

- **Chair actions per hour by kind.** How many actions the loop took
  each hour, split by kind: land, launch_epic, launch_decompose, rescue,
  relaunch, needs_chair, or standby. It reads the `chair_actions_by_hour`
  dataset.
- **Spend per landed task per day.** The day's model spend divided by
  the tasks the loop landed that day, next to the loop's own cost per
  landed task. It reads the `chair_spend_by_day` dataset.
- **Needs-chair backlog.** Which tasks are waiting on the chair right
  now, and how long the oldest one has waited. It reads the
  `chair_needs_backlog` dataset.

The loop makes no model calls itself, so its line in the spend panel is
zero by construction. It exists for contrast with the interactive
chair, which does call a model, so the two lines show a loop that costs
nothing next to one that does.

Chair reads the store only: `chair_actions`, `chair_ticks`, and
`node_calls`. It reads them through the same connection as the Coxswain
and Fleet dashboards, and never `chair.actions.jsonl`, `land.jsonl`, or
any other log file. None of its datasets reads `task_records`; a task
counts as landed from its `chair_actions` row, as the comment above the
chair datasets in `datasets.yaml` explains.

Like Fleet, bootstrap.py creates each Chair chart but still groups only
the Coxswain charts into a dashboard page. Open Charts from the top menu
and find a panel by its title.

## Verify Fleet and Chair

Bring the stack up and install the specs, from `deploy/superset`:

```sh
docker compose up -d
docker compose run --rm superset-dashboards
```

Then export the values from your `.env` and run the check from the
repository root.

```sh
python deploy/superset/check.py
```

It prints one line per chart, `<chart>: <rows> rows` or `<chart>: ERROR
<message>`, and exits 1 if any chart errors. Among its lines are the
five Fleet charts and the three Chair charts: Chair holder, epoch and
beat age; Lanes per host over time against capacity; Host state and
last login; Needs-chair items by cause; Weekly spend against the
ceiling; Chair actions per hour by kind; Spend per landed task per day;
Needs-chair backlog. A clean run exits 0.

Open each chart from the Charts list at <http://127.0.0.1:8088> to see
it rendered.

- **Chair holder, epoch and beat age** is healthy when it shows one row
  with a holder, host and epoch, and a beat age of a few seconds, well
  under the chair's heartbeat interval.
- **Lanes per host over time against capacity** is healthy when each
  host's line sits at or below its max-in-flight line.
- **Host state and last login** is healthy when every host reads a
  known state with a recent last login check.
- **Needs-chair items by cause** is healthy when it is empty, or short;
  a large oldest-waiting-hours value means the chair loop is behind.
- **Weekly spend against the ceiling** is healthy when the cumulative
  line rises but stays under the ceiling for the week.
- **Chair actions per hour by kind** is healthy when it shows a bar for
  most hours, mostly `land` and `standby`.
- **Spend per landed task per day** is healthy when the loop's line
  sits flat at zero next to the interactive chair's nonzero line.
- **Needs-chair backlog** is healthy when it is empty; a growing table
  means tasks are waiting longer than the loop is clearing them.

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
