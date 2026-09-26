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

## Land the work

When the lane ends, bring its work home:

```sh
cox runs fetch <run>
```

This brings the lane's task records and log to the chair. It also fetches
its branches, `agents/<run>/*`, from the host's repos. Then run
`cox runs land` as usual.

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

Other URL schemes and cloud-specific sign-in come from plugins in the
`coxswain.storage` entry-point group. The [Plugins](plugins.md) page lists
the groups. Core names no cloud vendor.

Traces and the lake still stay local unless these are set.

## What's next

Moving task records and the chair lock into the store is planned. Any
machine could then land.
