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

Each layer names its parent with `extends:`, and the chain is merged
parent-under-child. A repository can add a fourth, narrow layer of its own,
`.agent/cartridge.yaml`; see [Team
cartridge](../customize/team-cartridge.md#the-project-layer).

Each layer can add to or narrow what the layer below it says. None of
them can lower a risk or shorten a ramp.

## Fragments live in cartridge.d/

A layer is its `cartridge.yaml` plus any fragments in `cartridge.d/*.yaml`,
folded in sorted order after it. A fragment has the same keys as
`cartridge.yaml`; a later value wins a scalar conflict, so the file name
is part of the contract. Prose (a charter, conventions) lives in markdown
files named by the `context:` list, which concatenates base-first: a team
refines a base principle, it does not replace it. See
[Fragments](../customize/fragments.md) for how a team edits its own.

## Tighten-only

Two fields carry this rule: a write kind's **risk** (how dangerous the write is) and its **ramp** (how many clean runs before the harness stops asking). A child layer or fragment may raise the risk or lengthen the ramp; it may never lower either, and the loader refuses the resolve if it tries, naming the layer or fragment file.

The loader enforces this for risk and ramp, and for a few other rules at
load time: every required role is bound, every bound skill resolves to
exactly one body, every context path exists. Other values (budgets,
review tiers, `landing_areas.checks`) merge child-over-parent, so a team
sets its own; a team's checks list replaces the one it inherits. Keeping
risk and ramp tighten-only is what makes layering safe to compose: a
reviewer reading the base pack still knows the floor every team stands on
for what a write may do without a person.

## The skills and crew keys

A cartridge's `skills` key binds the roles a graph's nodes ask for (plan,
build, review_charter and so on) to skill bodies. Its `crew` key binds the
seats a person delegates to. Together they are the mechanism [Crew and
seats](crew-and-seats.md) describes generically, made concrete per team.
See [Skills](../customize/skills.md).

## Provenance

The base and local cartridges ship inside the coxswain checkout, under
`src/cartridges/cartridges/`. A workspace's `cartridges/base` and
`cartridges/local` are links into that folder, so they move with the
checkout's release tag. A team does not edit them in place.

Every fragment a run assembles can be traced back to the release that
shipped it. A team pack that wants something different extends base
or local (see [Team cartridge](../customize/team-cartridge.md)); it does
not patch the shipped cartridges directly.
