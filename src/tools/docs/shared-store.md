# The store

There is one store, and it is Postgres. It keeps the run records and the board: every intake, initiative and task, text and state.
Traces go to an S3-compatible object store, which is Garage in the recipe below.
You configure it with `cox setup store --store shared`.
A Postgres store needs the `postgres` extra on `cox`: `uv tool install --python 3.14 'coxswain[postgres]'`. Name every extra the machine uses, since a reinstall replaces them.

The older `files` store, a local sqlite file with the ticket files as the board, is retired.
`cox setup store --store files` and `cox setup store switch files` are refused by name.
Every command that touches the board refuses a profile whose store is not Postgres and says how to fix it.
A machine that still has an old sqlite store carries its rows over with `cox setup store switch shared`.

## Why one store

A single machine gets the same dashboards and the same backups as a fleet.
Nothing about the setup is specific to one box, so the tools behave the same on one machine as on ten.
When you add a second machine later, it points at the same store and no migration is needed.

## What shared sets up

Choosing shared writes three things: a storage URL, a traces URL and an object store block.
They go into a profile named `shared-store.yaml`, beside the settings file.
That profile is machine-local. Setup adds its name to the git exclude file, refuses to write it if git already tracks it, and creates it with owner-only permissions.
The choice itself is recorded in settings as `store.mode`.

The storage URL in that file contains the Postgres user and password. Treat the file as a secret.
Owner-only permissions and the git exclude exist for that reason. Do not copy it into a repository, a chat or a ticket.
The traces URL names a bucket. The object store block holds the endpoint, the region and the names of two environment variables, and holds no keys.

Setup reads the Postgres password and the S3 keys from environment variables that you name when you run it.
It never prints the values.
The object store block then points at two fixed variable names, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
Export the S3 key pair under those two names in every shell that runs cox against the shared store, because the keys are read from the environment each time and are not stored in the profile.

What the tools print is limited too. The setup summary names the store scheme, host, port and bucket, and never the user, password or keys.
The doctor row shows neither the URL nor its credentials.

## Carrying an old local store over

```bash
cox setup store switch shared
```

The switch copies an existing sqlite store and the local trace files into the shared store.
It leaves the source in place. Nothing is deleted or truncated in the store you came from, so you can inspect it afterwards.
A repeated switch is safe. The copy never drops anything, so running a switch again after an interrupted one is safe, and a second complete run reports zero newly copied.
If the store is already shared, the switch prints that there is nothing to do.

This copy is the only place outside the test suites where a sqlite store is opened.

## Checking the store with the setup doctor

`cox setup doctor` verifies the store in its `store` row.
The row shows the kind of store and the number of runs in it, for example `postgresql, 12 runs`.
When the store does not answer, the row fails and gives the reason. An unreachable host fails after at most five seconds.
The row never shows the URL or its credentials.

## A worker's scoped store role

```bash
cox setup store worker-role build-box-1
```

Run this with the store owner's URL in the provider profile. It creates the group role `cox_worker` if it is missing, and a login role `cox_w_build_box_1` that is a member of it.
The login role holds nothing of its own, so removing a machine means dropping one role.

The group role may update only the claim columns of `phase_queue`. It may insert into `step_results` and `review_rounds`, and insert and update its own run's records (`runs`, `phases`, `tasks`, `task_records`, `attempts`, `node_calls`, `gate_decisions`, `ledger` and the graph tables) and `workers`.
It may read `work_items`, `hosts`, `cartridge_snapshots`, `landing_records` and `schema_version`, and never write them.
It gets nothing on the land queue, the leases, the chair's tables or any table added later, and no right to create, alter or drop anything.

The login role and its password go into the units' environment file, mode 600, under `COX_WORKER_STORE_ROLE` and `COX_WORKER_STORE_PASSWORD`.
They never go into the provider profile, because every run's folder gets a copy of it. The password is never printed.

The password reaches Postgres only as its SCRAM-SHA-256 verifier, so the server's statement log never holds it.

Running the command again is safe. It keeps the password the file holds and leaves the grants exactly as listed: a grant added by hand is taken away, and a changed list is applied.
It also resets both roles: superuser, createrole, createdb, bypassrls and replication off, every direct grant revoked, and every membership but `cox_worker` revoked.
Run it again after a migration that adds a table a worker writes.

On Postgres before 15, everyone may create tables in the `public` schema, and no grant to the worker takes that away. The doctor row then fails and says so. Run `REVOKE CREATE ON SCHEMA public FROM PUBLIC` as the owner, or pass `--revoke-public-create` to this command.

A worker opens the store without migrating it. It refuses a store older than its code, or newer by a migration that is not additive.

On a machine whose environment file names a worker role, `cox setup doctor` adds a `worker store role` row.
The row connects as the role, checks that its allowed writes work and its forbidden ones are refused, and rolls every probe back.

## What brew and the installer do not do

Brew and the installer never start Postgres or Garage.
They install the tools. Running the services is your job, and running them for you is out of scope for this project.

If you want a local Postgres and Garage on one machine, the recipe below is an example you run yourself.
Both services are published on the loopback address only, so they are reachable only from this machine.
Credentials come from environment variables in your shell, and none are written into the files. Export them first and keep the values, because Postgres and Garage need the same ones on every start:

```bash
export POSTGRES_PASSWORD="$(openssl rand -hex 16)"
export GARAGE_RPC_SECRET="$(openssl rand -hex 32)"
export GARAGE_ADMIN_TOKEN="$(openssl rand -hex 32)"
```

Save this as `compose.yaml`:

```yaml
# Example only. You run this yourself; the tools never start it.
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: cox
      POSTGRES_DB: cox
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - cox-postgres:/var/lib/postgresql/data

  garage:
    image: dxflrs/garage:v1.0.1
    environment:
      GARAGE_RPC_SECRET: ${GARAGE_RPC_SECRET:?set GARAGE_RPC_SECRET}
      GARAGE_ADMIN_TOKEN: ${GARAGE_ADMIN_TOKEN:?set GARAGE_ADMIN_TOKEN}
    ports:
      - "127.0.0.1:3900:3900"
      - "127.0.0.1:3903:3903"
    volumes:
      - ./garage.toml:/etc/garage.toml:ro
      - cox-garage-meta:/var/lib/garage/meta
      - cox-garage-data:/var/lib/garage/data

volumes:
  cox-postgres:
  cox-garage-meta:
  cox-garage-data:
```

Save this beside it as `garage.toml`. It holds no secret, because Garage reads the RPC secret and the admin token from the two variables above.
The services listen on all interfaces inside their containers, and the published ports above are what keep them on loopback.

```toml
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1

rpc_bind_addr = "0.0.0.0:3901"
rpc_public_addr = "127.0.0.1:3901"

[s3_api]
s3_region = "garage"
api_bind_addr = "0.0.0.0:3900"

[admin]
api_bind_addr = "0.0.0.0:3903"
```

Start both with `docker compose up -d`. Then give Garage a layout, a bucket and a key with its command line. Replace `<node id>` with the id that `status` prints:

```bash
docker compose exec garage /garage status
docker compose exec garage /garage layout assign -z dc1 -c 1G <node id>
docker compose exec garage /garage layout apply --version 1
docker compose exec garage /garage bucket create cox-traces
docker compose exec garage /garage key create cox-key
docker compose exec garage /garage bucket allow --read --write --owner cox-traces --key cox-key
```

`key create` prints a key id and a secret once. Export them as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, the two names the profile points at.
For `cox setup store --store shared`, give setup the Postgres host, port and database from the compose file, the bucket `cox-traces`, the region `garage`, and the endpoint on the loopback address at port 3900.
Tell setup the names of the environment variables that hold the Postgres password and the S3 keys, not their values.
