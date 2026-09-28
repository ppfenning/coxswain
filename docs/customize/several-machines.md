# Several machines

This page shows how to run Coxswain lanes on more than one machine, launch
them from one place, and bring their work home to land. It assumes you have
run Coxswain on one machine.

## The model

The chair is the machine where you run `cox`. It keeps the workspace, which
holds your tickets and task records. It also does all landing. Other
machines only run lanes.

All machines share one run store. The store is a Postgres named by
`storage_url` in the provider profile. Install the driver with the postgres
extra:

```sh
pip install 'coxswain-tools[postgres]'
```

With a local SQLite store, everything stays on one machine.

## Set up a lane machine

Do these steps on each machine that will run lanes.

1. Clone the component repos. Also clone each repo your initiatives target,
   at the same absolute path as on the chair, with its `.venv` and check
   tools. The lane reads the initiative's `repo:` path unchanged.
2. Install cox with the postgres extra, as above.
3. Use the same provider profile. Its `storage_url` names the shared
   Postgres.
4. Put `claude` on the PATH of a non-interactive ssh shell, and log in.
   Set a git `user.name` and `user.email`.
5. Run `cox setup doctor`.

The `store` row of the doctor shows the store's kind and run count. It never
shows the URL.

From the chair, `cox setup doctor --host <name>` also runs `claude auth
status` on the host. It adds a `claude auth` row. The row reads like this
when the login is good:

```text
claude auth      ok
```

When it is not, the row reads like this, and the doctor exits 1:

```text
claude auth: not logged in on the host (run claude auth login there)
```

Run `claude auth login` on the host. A login can lapse on a machine nobody
types on, so a lane machine that worked last week can fail this check.
`cox route launch --on <host>` runs the same check first and refuses to
launch when it fails.

A login watchdog also checks every lane host's Claude login over ssh every
30 minutes, timing out after 60 seconds, and records the result with the
host's beat. A host whose login has lapsed gets no new lanes; it stays in
the hosts table and its running lanes continue.

## Name the lane machines on the chair

On the chair, list the lane machines in the routing profile:

```yaml
lane_hosts:
  - {name: box2, ssh: me@box2, workspace_dir: /home/me/workspace}
```

`workspace_dir` is the workspace's absolute path on that machine.

`ssh` is a destination only. Put a port, user or key in your ssh config under
a Host alias, and use the alias here. Connect once by hand so the host key is
known.

Run the doctor on a lane machine from the chair with `--host`:

```sh
cox setup doctor --host box2
```

The doctor runs there over your own ssh config. The profile holds no keys.

## Add a host to the shared table

Add the host to the hosts table in the shared store so placement can fill
it:

```sh
cox host add box2 --ssh me@box2 --capacity 3
```

`--capacity` is the lanes the host may run at once. An optional `--weight`
sets its share of new placements, and an optional `--capabilities` names
the list it can be matched on.

`cox host drain box2` stops launching on the host while its live lanes
finish; `cox host activate box2` makes it a lane host again. `cox host
list` prints one line per host: its state, capacity, beat age, and login.

## Launch a lane

Launch an epic on a lane machine with `--on`:

```sh
cox route launch epic --initiative work/<id> --on box2
```

The command allocates the run id on the chair. It copies `work/<id>` to the
host with rsync. It then starts the lane there over ssh.

## Watch the lanes

The docket, `cox route status` and `cox runs top` list lanes that other
machines hold. Each one reads like this:

```text
<run> (on box2, since HH:MM, heartbeat Ns ago)
```

The docket is `cox route context`. Liveness comes from a lease in the shared
store. A second run of the same epic is refused on any machine.

A lane counts as lost only when its host has gone quiet and its own lease
heartbeat has been quiet for 10 minutes; a lost lane is marked lost and
relaunched on another host. Placement weighs each host's weight and
capabilities when it fills free lanes.

## Make a lane machine the resident chair

A lane machine can run the chair loop itself. `cox chair service --install
--host <name>` prints the steps that put the loop on that host as a
systemd user service; add `--apply` to run them instead of printing. `cox
chair service --status --host <name>` reports it.

Two steps stay with a person: `loginctl enable-linger` and `systemctl
--user enable --now`.

## Land the work

When the lane ends, bring its work home:

```sh
cox runs fetch <run>
```

This brings the lane's task records and log to the chair. It also fetches
its branches, `agents/<run>/*`, and its phase branches, `epic/<initiative>/*`,
from the host's repos, and the fetched run's approvals reach the ticket
files. Then run `cox runs land` as usual.

The [autonomous chair](autonomous-chair.md) does the fetch itself. It runs
`cox runs fetch` before it lands a remote lane.

The chair's copy of a ticket stays at its old state while the lane runs. The
land marks it done.

## Shared storage

With no settings, traces live under `runs/traces` and the Iceberg lake
under `runs/lake` on each machine. One machine needs nothing more.

For several machines, point `traces_url` and `lake_url` in the provider
profile at a shared path. An NFS mount works, if every machine has it at
the same absolute path. No new service is needed.

For object storage, use `s3://` URLs with a self-hosted S3-compatible
server, such as Garage. Add an `object_store` block to the provider
profile:

```yaml
object_store:
  endpoint: http://garage.lan:3900
  region: garage
  access_key_env: GARAGE_ACCESS_KEY_ID
  secret_key_env: GARAGE_SECRET_ACCESS_KEY
  path_style: true
```

The profile names the environment variables that hold the key and secret.
It never holds the values. A literal key in the profile is refused.
`cox lake doctor` shows the endpoint and whether each variable is set.

### A Garage server

A single-node Garage on the tailnet needs one profile block. Replace `<host>`
and `<bucket>` with your own:

```yaml
object_store:
  endpoint: http://<host>:3900
  region: garage
  access_key_env: GARAGE_ACCESS_KEY_ID
  secret_key_env: GARAGE_SECRET_ACCESS_KEY
  path_style: true
traces_url: s3://<bucket>/traces
lake_url: s3://<bucket>/lake
```

Garage 1.1 also needs two SDK settings. Without them the pyarrow S3 client
fails every upload with `invalid checksum algorithm`. Set both to
`when_required`:

- `AWS_REQUEST_CHECKSUM_CALCULATION=when_required`
- `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required`

Keep the key, the secret and these two settings in one env file. Write them
as `export NAME=value` lines and set the mode to 600:

```bash
export GARAGE_ACCESS_KEY_ID=<key>
export GARAGE_SECRET_ACCESS_KEY=<secret>
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
```

```bash
chmod 600 <the EnvironmentFile path>
```

The file's path is the one `cox chair service` uses as `EnvironmentFile`.
Source it from the login shell's startup file on every machine. For zsh that
is `~/.zshenv`:

```bash
. <the EnvironmentFile path>
```

Lanes started over ssh get their environment from that file and from nothing
else. A file sourced only from an interactive startup file does not reach them.

A run reads the profile when it starts. It reads it again when it writes its
trace at the end. A run started before the file existed fails its trace write
with `object_store names env var ... which is not set`. The first trace in the
bucket comes from the first run started after the change.

Check the setup in three places:

- `cox lake doctor` shows the endpoint and whether both variables are set.
- `cox setup doctor` has a traces row that names the endpoint.
- The bucket listing shows a trace after one run.

Other URL schemes and cloud-specific sign-in come from plugins in the
`coxswain.storage` entry-point group. The [Plugins](plugins.md) page lists
the groups. Core names no cloud vendor.

Traces and the lake still stay local unless these are set.

## Land from any machine

By default the ticket files hold each task's state, which is
`work_state: files`. When the provider profile's `storage_url` names a
shared Postgres database that answers, the loop reads work items from the
store instead; `cox route status` then prints `work state: store` first. A
local SQLite store keeps the files as the board. Only the chair lands from
the ticket files; any machine can land once the store is the board.

With the store as the board, a land on any machine checks that the task is
approved in the store. It holds a lease named `land:<task>` while it
merges. It moves the task to done with a compare-and-set. If another
machine landed the task first, the land stops before it merges anything.
The ticket file's `state:` is updated after.

The ticket files become a cache. After editing them by hand, `cox route
import` writes them to the store, and `cox route drift` compares the two.
This command also shows how they differ from the store:

```sh
python -m harness.store_cli regenerate-states <work_dir> --initiative <id>
```

Add `--apply` to rewrite their `state:` lines.

When the shared store already holds the lane's task records,
`cox runs fetch` brings only the lane's branches.

Every machine that lands needs a checkout of the workspace. Ticket bodies
stay in git.
