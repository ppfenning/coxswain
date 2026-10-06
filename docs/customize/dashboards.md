# Dashboards

This page shows how to run Apache Superset over your Coxswain store and
lake, read the Coxswain dashboard, and add a chart of your own. It
assumes you have never used Superset.

## What Superset is

Superset is a web app for charts and dashboards over SQL. You point it at
a data source, write a query, and save the result as a chart. Charts sit
together on a dashboard. Coxswain ships a Superset setup in
`deploy/superset` that reads the store and the lake over the network.

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
  last call is two days old and it has no landing in the store's
  `task_records` table.

A task landed when its row in the store's `task_records` table has
`record_json.landed` set: `true` on an older record, or an object such
as `{"at": ..., "pr": ...}` on a newer one. `daily_efficiency`,
`landed_tasks` and `task_outcomes` each carry a single `requires: lake`
in `datasets.yaml`, so they are created when the lake probe answers, and
they read the store too, through `{{store:task_records}}`: a store that
is down makes their charts fail at query time, not at dataset creation.
See [The lake](#the-lake).

The never-landed bar can overstate waste for a task that landed before
`task_records` started tracking it in your workspace. unknown: when a
given workspace's `task_records` table starts holding landed rows; the
store carries no fixed epoch the way `runs/land.jsonl`'s first line once
did.

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
- `COXSWAIN_S3_ENDPOINT` is the S3-compatible endpoint of the
  Garage-backed Iceberg lake. Empty skips the lake entirely.
- `COXSWAIN_S3_KEY_ID` is the access key id for that endpoint.
- `COXSWAIN_S3_SECRET` is the secret access key for that endpoint.
- `COXSWAIN_S3_REGION` is the region DuckDB sends with each S3 request.
  Garage accepts any string; it defaults to `garage` when left empty.

`SUPERSET_SECRET_KEY` and every admin variable must be set, and compose
refuses to start when one is empty or still reads `change-me`.
`COXSWAIN_STORE_URL` and the `COXSWAIN_S3_*` variables carry no such
check: they default to empty, and an empty `COXSWAIN_S3_ENDPOINT` just
skips the lake. Never commit `.env`.

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
each dataset, with its charts, until the source it requires answers a
readiness probe. Run it again after a spec changes, or once the store or
the lake first answers. A second run changes nothing when the specs
already match Superset.

Readiness comes from a probe, not a file check. Before installing
anything, bootstrap runs one `SELECT ... WHERE false` per required
table through SQL Lab, never `LIMIT 0`; a `WHERE false` select fails on
a missing table or column exactly like a real query would, and reads no
row when it succeeds. A SQL Lab answer whose JSON body carries an
`errors` key counts as absent, whatever HTTP status it arrived on: 200,
400, 422 and 500 all read the same way. A 401 or 403, or a network
error the HTTP call itself raises, is not treated as absent; it
propagates and stops bootstrap instead of quietly skipping a dataset.

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

Fleet reads the store only: `chair_actions`, `hosts`, `leases`, `runs`,
and `node_calls`. An earlier ticks table that used to sit alongside
`chair_actions` is gone from this list; nothing ever wrote to it, so no
Fleet dataset reads it. Fleet reads these tables through the same
connection as the Coxswain and Chair dashboards, `sqlite_scan` of
`cox.db` or an attached Postgres catalog, and never
`chair.actions.jsonl`, `land.jsonl`, or any other log file. None of its
datasets reads `task_records`.

The host-state panel is skipped until the `hosts` table is readable.
`fleet_host_state` requires `store`, the same single requirement every
Fleet dataset carries, and bootstrap installs a dataset only once its
required source answers its probe. Once a run has written to `hosts`,
rerun `docker compose run --rm superset-dashboards` to install the
panel.

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
busy AS (
  SELECT hours.hour_start, r.host, COUNT(*) AS lanes
  FROM hours
  JOIN r ON r.launched_at <= hours.hour_end
        AND COALESCE(r.ended_at, CAST(now() AT TIME ZONE 'UTC' AS TIMESTAMP)) >= hours.hour_start
  GROUP BY ALL
)
SELECT busy.hour_start AS hour, busy.host, busy.lanes, hosts.capacity
FROM busy
LEFT JOIN {{store:hosts}} AS hosts ON hosts.name = busy.host
ORDER BY hour, host
```

`fleet_host_state` feeds **Host state and last login**:

```sql
SELECT name, state, capacity, CAST(beat_at AS TIMESTAMP) AS beat_at,
       CAST(json_extract(versions_json, '$.login_ok') AS BOOLEAN) AS login_ok,
       CAST(json_extract_string(versions_json, '$.login_checked_at') AS TIMESTAMP) AS login_checked_at
FROM {{store:hosts}}
```

`fleet_needs_chair_by_cause` feeds **Needs-chair items by cause**:

```sql
WITH flagged AS (
  SELECT target, json_extract_string(action_json, '$.reason') AS reason, CAST(ts AS TIMESTAMP) AS flagged_at,
         ROW_NUMBER() OVER (PARTITION BY target ORDER BY CAST(ts AS TIMESTAMP) DESC) AS rn
  FROM {{store:chair_actions}} WHERE kind = 'needs_chair'
),
landed AS (
  SELECT target, CAST(ts AS TIMESTAMP) AS landed_at
  FROM {{store:chair_actions}} WHERE kind IN ('land', 'land_phase') AND status = 'landed'
),
waiting AS (
  SELECT f.reason, f.flagged_at
  FROM flagged AS f
  WHERE f.rn = 1
    AND NOT EXISTS (
      SELECT 1 FROM landed AS l WHERE l.target = f.target AND l.landed_at > f.flagged_at
    )
)
SELECT reason AS cause, COUNT(*) AS items,
       MAX(date_diff('minute', flagged_at, CAST(now() AT TIME ZONE 'UTC' AS TIMESTAMP))) / 60.0 AS oldest_waiting_hours
FROM waiting
GROUP BY reason
```

`fleet_weekly_spend` feeds **Weekly spend against the ceiling**:

```sql
SELECT date_trunc('week', CAST(ts AS TIMESTAMP)) AS week, SUM(cost_usd) AS cost_usd
FROM {{store:node_calls}}
WHERE CAST(ts AS TIMESTAMP) >= date_trunc('week', CAST(now() AT TIME ZONE 'UTC' AS TIMESTAMP)) - INTERVAL 12 WEEK
GROUP BY 1
ORDER BY 1
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

Chair reads the store only: `chair_actions` and `node_calls`. The same
ticks table Fleet no longer lists is gone from here too; nothing ever
wrote to it, so no Chair dataset reads it. Chair reads these tables
through the same connection as the
Coxswain and Fleet dashboards, and never `chair.actions.jsonl`,
`land.jsonl`, or any other log file. None of its datasets reads
`task_records`; a task counts as landed from its `chair_actions` row,
as the comment above the chair datasets in `datasets.yaml` explains.

Like Fleet, bootstrap.py creates each Chair chart but still groups only
the Coxswain charts into a dashboard page. Open Charts from the top menu
and find a panel by its title.

## Crew

The Crew dashboard has eleven charts. They fall into five groups: cost
and turns, approval and attempts, verdicts, quarantines, and efficiency.
Crew reads the store only, through the `crew_calls`, `crew_quarantines`,
`crew_tasks`, `crew_reviews` and `crew_task_roles` datasets.

Cost and turns:

- **Crew cost per day by role.** How much each role spent each day.
- **Crew cost per day by model.** How much each model alias spent each
  day.
- **Crew turns per day by role.** How many turns each role took each day.

Approval and attempts:

- **First-try approval rate by builder seat.** What share of each
  builder seat's tasks were approved on the first build attempt.
- **Build attempts per approved task by builder seat.** How many build
  attempts each builder seat needed on the tasks that were approved.

Verdicts:

- **Crew reviewer verdict mix.** How the charter reviewer's verdicts
  split across the adversary's verdicts.
- **Arbiter sides with the adversary.** Which side the arbiter took on
  the tasks it ruled on.

Quarantines:

- **Quarantines by role and cause, last 14 days.** Which roles had
  attempts quarantined, and why.
- **Quarantines by repository and cause, last 14 days.** Which
  repositories had attempts quarantined, and why.

Efficiency:

- **Cache-read share by role.** What share of each role's input tokens
  came from the cache over the last two weeks.
- **Cost per approved task per role.** What each role spends for every
  approved task.

Some terms are easy to misread. A task is first-try when it is approved
with exactly one build attempt. The builder seat is the model alias of
the task's build calls, taken from the earliest build call. A quarantine
is an attempt of kind refused, unverified, infra or dropped, and an
attempt with no cause is counted as unclassified. The arbiter chart
leaves out tasks with no arbitration, so it counts only tasks the arbiter
ruled on. Cost per approved task divides a role's cost on approved tasks
by the number of approved tasks.

The datasets state fallbacks that the charts inherit. No approved marker
has been found in the store, so a task counts as approved when its final
review verdict is `approve`. That verdict is the arbiter's when the
arbiter ruled and the reviewer's otherwise. It does not read the landed
state, so an approved task that has not landed yet counts as approved.
A quarantined attempt takes its role from the last model call for the
same run and task. It takes its repository from `runs.repo`, joined on
the run. An attempt with no matching call or run keeps its row with an
empty role or repository. The datasets mark as unknown whether `runs.repo`
exists, and that `approve` is the stored verdict string.

## The lake

The lake carries six tables: `runs`, `phases`, `node_calls`,
`gate_decisions`, `ledger`, and `traces`. Only `node_calls`, `runs` and
`traces` back a dataset today.

`calls` and `calls_by_day` read `{{lake:node_calls}}`. `runs` and
`lanes_by_hour` read `{{lake:runs}}`. `traces` reads `{{lake:traces}}`.
`daily_efficiency` and `task_outcomes` read `{{lake:node_calls}}`, and
`landed_tasks` reads `{{lake:node_calls}}` and `{{lake:runs}}`; all
three also read the store's `task_records` table, through
`{{store:task_records}}` (see [Efficiency](#efficiency)).

Every Fleet and Chair dataset reads the store only, through
`{{store:<table>}}`, and requires `store` rather than `lake`. Two more
datasets, `attempts` and `task_verdicts`, read the store only too but
still carry `requires: lake` in `datasets.yaml`, so bootstrap skips them
until the lake probe succeeds even though their SQL never touches it.

The `traces` dataset now reads `{{lake:traces}}` instead of scanning
local Parquet files, so a trace that graphs has pruned from disk still
shows up in the tool-use chart; the lake keeps its history after the
local file is gone.

A dataset with `requires: lake` is skipped, with its charts, until the
lake probe succeeds, the same way a `requires: store` dataset waits on
the store probes.

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

There is no `COXSWAIN_RUNS_DIR` and no `/data/runs` mount any more. No
service in `docker-compose.yml` mounts a runs directory; the store and
the lake are read over the network instead. The store connection
attaches Postgres with `READ_ONLY` (`attach_sql` in
`superset_config.py`); the lake connection only creates DuckDB views
over `iceberg_scan` (`lake_view_sql`), which has no write path. Superset
can query both but cannot change them.

With `COXSWAIN_STORE_URL` and `COXSWAIN_S3_ENDPOINT` set, every dataset
runs on any machine that reaches the store and Garage: that includes
`daily_efficiency`, `landed_tasks` and `task_outcomes`, which read
landed rows from the store's `task_records` table, not from a local
file. Queries read the store and the lake each time they run, so a
refresh shows new data with no import step.

Superset keeps its own users, datasets, and charts in a separate Docker
volume. Removing that volume loses your charts and never touches the
store or the lake.

## Point the dashboard at Postgres

Set `COXSWAIN_STORE_URL` in `.env` to your Postgres store's URL. The URL
below is a placeholder.

```sh
COXSWAIN_STORE_URL=postgresql://reader:change-me@db.example:5432/coxswain
```

Use a read-only Postgres role. The connection is attached read-only, but
the role is the real limit. The store's tables must sit in the `public`
schema of the database the URL names. A table in another schema fails
when a chart queries it.

With the variable set, the datasets read the store through an attached
catalog named `store`. Their SQL reads `store.public.<table>`. With it
unset or empty, dataset SQL still falls back to a `sqlite_scan` of
`/data/runs/cox.db`, but nothing mounts that path any more, so the store
probes fail and bootstrap skips every store dataset, with its charts. In
practice the deploy needs `COXSWAIN_STORE_URL` set.

The `superset-dashboards` one-shot writes that SQL into the datasets.
`docker compose up -d` runs it, as described above. If it did not run,
the datasets keep their old SQL. Run it by hand after changing the
variable.

```sh
docker compose run --rm superset-dashboards
```

Readiness in Postgres mode comes from the same probes as any other
mode. Bootstrap runs the chair, hosts and lake probes through SQL Lab on
the database Superset already has; it never checks for a local file.
Until those probes succeed it skips the datasets that need them, with
their charts, the same way it does on the first run against a new
server.

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

The same hook loads the lake, and only after that `ATTACH`. With
`COXSWAIN_STORE_URL` unset it returns before the lake step, whatever the
`COXSWAIN_S3_*` variables hold; the lake's table list is also read from
the attached store, from `store.public.iceberg_tables`. Set
`COXSWAIN_STORE_URL` and `COXSWAIN_S3_ENDPOINT` together to read the
lake. The "Shared storage" section of
[Several machines](several-machines.md#shared-storage) puts traces and
the lake on Garage, through `traces_url`, `lake_url` and an
`object_store` block in the provider profile.

## Other data sources

The Superset image queries with DuckDB, so a dataset can read any source
DuckDB reads.

**A Postgres store.** See the section above, which sets it up without
putting the password in any SQL.

**Traces.** Traces come from the lake now, through `{{lake:traces}}`;
see [The lake](#the-lake). No dataset needs a shared Parquet location
any more.

**A local file.** No container mounts a host directory. A dataset that
must read one needs a volume added to `docker-compose.yml` first, which
is a change to the deploy files and ties the deploy to the machine that
holds the file.
