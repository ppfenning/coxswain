# Team cartridge

This page settles how a team gets its own cartridge: run `cartridge init
<team>`, then decide whether you're extending `local` or `base`.

## cartridge init <team>

```sh
cartridge init my-team --cartridges-dir <workspace>/cartridges
```

This creates `<workspace>/cartridges/my-team/` from the example team: a
`cartridge.yaml` and a `context/` folder for your charter and
conventions. It links `base` and `local` beside it, pointing into the
checkout's `src/cartridges/cartridges/`. It then prints the profile lines
that name the new team. `--extends base` makes it extend base instead of
local; `--dry-run` prints the plan and writes nothing.

It does not write your charter or conventions for you. Those are yours to
write, because they say what your team actually believes.

## The keys

A team cartridge's top-level keys are the same shape as base's: write
kinds with their risk and ramp, `policy`, `landing_areas`, `context`, and
a `crew` key naming which skills bind to which seats. Fragments under
`cartridge.d/` add to them; see [Fragments](fragments.md). A team may not
lower a risk or shorten a ramp that `local` or `base` set; see
[Cartridges](../methodology/cartridges.md#tighten-only).

## Checks

`landing_areas.checks` lists the commands the harness runs on every
build, as `{name, cmd}` entries, with an optional `timeout` (seconds) and
`fix` (a command that repairs what the check found). A repository can add
its own checks on top with a root `.agent-checks` file. See
[Landing](../methodology/landing.md#checks-are-evidence).

## Checks by gate

Three lists hold checks, one per gate. `landing_areas.checks` holds the task checks. `landing_areas.level_checks` holds the checks for the level gate. `landing_areas.ticket_checks` holds the checks for the ticket gate.

All three use the `{name, cmd}` shape, with names unique inside each list. The two new lists are optional. A cartridge that omits them has no level or ticket checks, and `checks` reads as it always has.

An integration suite that needs exclusive use of a resource belongs in `level_checks` or `ticket_checks`, because those run once for the level or ticket and not beside every task. The harness reads the two new lists in separate changes.

## Landing granularity

`landing_areas.land_at` sets where landing happens: `ticket`, `level` or `task`.
The default is `ticket`, one pull request per initiative. The approval gate
applies only at the chosen granularity.

```yaml
landing_areas:
  land_at: level
```

A repository overlay may set the same key, and it replaces the team value for
that repository. An initiative can override both with `land_at:` in the
frontmatter of its `initiative.md`. See
[Landing](../methodology/landing.md#landing-granularity) for what each value
lands and when.

## Target branch

`landing_areas.target_branch` names the branch initiatives land into by default.

```yaml
landing_areas:
  target_branch: release/next
```

An epic definition or an initiative can override it. See
[Landing](../methodology/landing.md#target-branch).

## The project layer

A repository can also carry `.agent/cartridge.yaml`. The harness reads it
for runs against that repository and folds it in last. It may set only
`description`, `context`, `policy.review_tier`, `policy.cost.level`,
`landing_areas.checks` and `landing_areas.land_at`. Its checks list replaces
the team's list for that repository; `.agent-checks` still adds to it. Any
other key is refused.

The project layer may only tighten review: it may lower a `review_tier`
threshold, add to `tier2_surfaces` and remove from `tier0_patterns`, never
the reverse. It may move `policy.cost.level` toward `strict`, never toward
`liberal`.

## Test database

`landing_areas.test_database` gives each build lane its own test database. A
repository sets it in `.agent/cartridge.yaml`. It takes five keys. `env` is
the name of the environment variable that carries the database URL to the
harness. `template` is the URL, with one `{lane}` placeholder. `drop` is an
optional command that removes the database. `schema` and `create` are
optional keys that build the database, described below.

Coxswain replaces `{lane}` in `template` with the lane id. The text after
`{lane}` must not be empty, and the database name keeps that literal suffix.
In `drop`, `{name}` stands for the resulting database name.

When `create` is not set, the project harness is expected to create the
database on first use. When `create` is set, coxswain runs it to build the
database, as described below. When `drop` is set, coxswain runs it to remove
the database. When `drop` is absent, coxswain lists the database name for
cleanup on the run result.

Two more optional keys let coxswain build the schema in the new database.
`schema` is a path relative to the repository. It must not be empty, must not
start with a slash and must not contain a `..` segment. `create` is the
command that builds the database. It must hold `{name}`, which stands for the
database name, and it may hold `{schema}`, which stands for the `schema` path.
`create` requires `schema`. Coxswain runs `create` in the lane worktree, so a
relative `schema` path resolves there. A later run can meet a database that
already exists, so `create` must tolerate an existing database. When `schema`
is set without `create`, coxswain builds nothing and records that no create
command was configured.

The outcome of `create` is a record for the lane. It holds the database name,
a status and the last 500 characters of the command output. The status is
`created` when the command exits with zero, `failed` when it exits non-zero,
cannot start or runs past 60 seconds, and `skipped` when no command was
configured. Coxswain returns a failure as a `failed` record and does not
raise it. A `failed` or `skipped` record does not change the database name,
which always comes from `template` and the lane id.

Two fields already show the lane database in a run. The task record carries
the database name under `test_database`. The run result lists the outcome of
each drop under `lane_databases`. The `create` record is returned to the
caller that ran `create`. No field of the task record or the run result
carries it yet, so a `failed` status is not visible there.

```yaml
landing_areas:
  test_database:
    env: DATABASE_URL
    template: postgresql://localhost/app_{lane}_test
    drop: dropdb {name}
```

A repository with no `test_database` setting behaves as before. When `create`
is not set, building the schema in the new database is the project's own
concern.

### Integration checks

`landing_areas.integration_checks` is a list of checks that run against a
real database. Each entry has the same shape as an entry in
`landing_areas.checks`: a `name`, a `cmd` and an optional `timeout`. Like the
other check lists it adds to what a layer below it set. A `cmd` may hold the
placeholder `{packages}`. Coxswain replaces it with the packages the change
touched, sorted and separated by spaces.

The repository overlay in `.agent/cartridge.yaml` is refused, with a message
that names `landing_areas.integration_checks`, when any of these hold. The
value is not a list. The list is empty, so leave the key out instead of
writing `[]`. An entry is not a mapping or lacks `name` or `cmd`. A `name` or
`cmd` is not a non-empty string. Two entries share a `name`.

A package is named from a changed path. Under a source tree, which is `src`,
a path with three or more segments belongs to the package named by its first
two segments, so `src/graphs/harness/x.py` belongs to `src/graphs`. Any other
path belongs to the package named by its first segment. A changed test file
counts as touching its own package. When the list of changed paths is empty,
no package is named and the selection holds no check.

Integration results show up in three places.

- The task gate. It runs only when `integration_checks` is not empty and the
  lane has a private test database. A failure shows up as a task that is not
  approved.
- The level gate. It runs only when `integration_checks` is configured and a
  `test_database` is set. It uses its own private database, never the database
  of a lane. A run that lands nothing still gets the level gate. That means
  landing nothing does not exempt a run from the gate. The gate still selects
  checks by the same package rule, so it holds no check when its list of
  changed paths is empty.
- The run summary. It counts integration results apart from unit results, so
  a green unit count never hides a failing integration check.

A lane has a private test database when `test_database` is set, because the
database name comes from `template` and the lane id. A `failed` or `skipped`
`create` record does not take that database away. Without a private lane
database the task gate does not run. The level gate needs `test_database` and
does not depend on any lane. A repository that sets no `test_database` gets
neither gate, whatever `integration_checks` holds.

```yaml
landing_areas:
  test_database:
    env: DATABASE_URL
    template: postgresql://localhost/app_{lane}_test
    drop: dropdb {name}
    schema: db/schema.sql
    create: ./scripts/create-db.sh {name} {schema}
  integration_checks:
    - name: db-integration
      cmd: ./scripts/integration.sh {packages}
      timeout: 600
```

A repository with none of `schema`, `create` and `integration_checks`
behaves as before.

## Extending local or base

Most teams extend `local` (the default) rather than reaching past it to
`base`. Extend `base` directly only when your change is genuinely
independent of anything local has already decided; reaching past a layer
that already narrowed something usually means you're fighting the layer
instead of using it.

## What lands where

A team cartridge doesn't replace base or local; it's resolved on top of
them at run time, layer by layer. See [Fragments](fragments.md) for how
the pieces inside your cartridge are folded, and [Policy](policy.md) for
the values most teams change first.
