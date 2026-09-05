# Cartridges

This page settles how a team's own standards enter a run without forking
the platform: a cartridge layers base, then local, then team context, in
fragments that only ever tighten what came before.

## Three layers

A cartridge is a packaged bundle of context for a team or a repo: its
conventions, its charter, its thresholds. It resolves in three layers,
applied in order:

- **base** — the substrate's own conventions and epic model, the same for
  every team.
- **local** — whatever this repository has already layered on top of
  base.
- **team** — the specific team's own charter and thresholds, added last.

Each layer can add to or narrow what the layer below it says. None of
them can loosen it.

## Fragments live in cartridge.d/

A layer is not one file; it is a directory of fragments,
`cartridge.d/*.yaml`, read in sorted order. Each fragment is one piece of
context — a convention, a charter section, a style note — that gets
assembled into a seat's prompt for a run. Sorted order means the file
name is part of the contract: rename a fragment and its place in the
assembled prompt changes with it. See
[Fragments](../customize/fragments.md) for how a team edits its own.

## Tighten-only

Two fields carry this rule most often: a write kind's **risk** (how dangerous the write is) and its **ramp** (how many clean runs before the harness stops asking). A child layer or fragment may raise the risk or lengthen the ramp; it may never lower either, and the loader refuses the resolve if it tries, naming the layer or fragment file.

A team layer can raise a risk threshold, shrink a budget, or add a
required check. It cannot lower a threshold, raise a budget past base's
ceiling, or drop a check a layer below already required. This is what
makes layering safe to compose: a team pack can only make the substrate
stricter for itself, never looser, so a reviewer reading the base pack
still knows the floor every team stands on.

## The crew key

A cartridge's `crew` key is where a team names which skills bind to which
seats and roles for its own runs — the mechanism [Crew and
seats](crew-and-seats.md) describes generically, made concrete per team.

## Provenance

The cartridges component is itself a pinned dependency, not something a
team edits in place:

```
cartridges = { repo = "ppfenning/coxswain-cartridges", tag = "v0.1.0-beta.1", required = true }
```

Every fragment a run assembles can be traced back to the cartridge tag
that shipped it. A team pack that wants something different extends base
or local (see [Team cartridge](../customize/team-cartridge.md)); it does
not patch the pinned component directly.
