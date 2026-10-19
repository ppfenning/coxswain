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

## The project layer

A repository can also carry `.agent/cartridge.yaml`. The harness reads it
for runs against that repository and folds it in last. It may set only
`description`, `context`, `policy.review_tier`, `policy.cost.level` and
`landing_areas.checks`. Its checks list replaces the team's list for that
repository; `.agent-checks` still adds to it. Any other key is refused.

The project layer may only tighten review: it may lower a `review_tier`
threshold, add to `tier2_surfaces` and remove from `tier0_patterns`, never
the reverse. It may move `policy.cost.level` toward `strict`, never toward
`liberal`.

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
