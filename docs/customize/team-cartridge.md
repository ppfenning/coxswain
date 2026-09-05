# Team cartridge

This page settles how a team gets its own cartridge: run `cartridge init
<team>`, then decide whether you're extending `local` or `base`.

## cartridge init <team>

`cartridge init <team>` creates the directory structure for a new team
cartridge: a place for your fragments, and the keys a cartridge needs to
resolve. It does not populate your charter or conventions for you — those
are yours to write, because they're supposed to say what your team
actually believes, not fill in a generic placeholder.

## The keys

A team cartridge's top-level keys are the same shape as base's: risk and
ramp thresholds, a `crew` key naming which skills bind to which seats,
and whatever fragments the team adds under `cartridge.d/`. What differs
is that a team's values can only tighten what `local` and `base` already
set — see [Cartridges](../methodology/cartridges.md) for the
tighten-only rule this enforces.

## Extending local or base

Most teams extend `local` — whatever this repository has already set up
— rather than reaching past it to `base`. Extend `base` directly only
when your change is genuinely independent of anything local has already
decided; reaching past a layer that already narrowed something usually
means you're fighting the layer instead of using it.

## What lands where

A team cartridge doesn't replace base or local; it's resolved on top of
them at run time, layer by layer. See [Fragments](fragments.md) for how
individual pieces inside your cartridge get assembled, and
[Policy](policy.md) for the specific values most teams change first.
