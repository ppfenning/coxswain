# agent-cartridges

A portable substrate for running agent graphs against *your* team's rules.

Graphs reference abstract **roles** (`plan`, `build`, `review_charter`). A
**cartridge** binds those roles to real skills, names where writes land, and
carries the team's own written conventions. The same graph runs for any team
that can fill the contract — swapping trackers, style charters, or model
providers is an edit to one file, not a rewrite.

Nothing writes to a system of record without a human decision until that
specific kind of write has earned it, measured against an append-only ledger.

## Why cartridges

Most agent tooling hardcodes an employer into the automation: this tracker,
that board, those conventions. Then the team changes, or you do, and the
tooling is worthless. Here the seam is explicit and enforced — a graph that
hardcodes a domain constant fails its own acceptance test.

```
graphs/            portable. reference roles, never skills or vendors.
core/              pure substrate: merge, policy, manifest, ledger.
cartridges/base/   the contract: roles, write-kind taxonomy, autonomy policy.
cartridges/<team>/ bindings: which skill fills each role, where writes land.
providers/         tier -> model. the vendor axis, isolated.
```

## The two axes

| Axis | Changes when | Lives in |
|---|---|---|
| **Domain** | you change teams, trackers, or conventions | `cartridges/` |
| **Vendor** | you change model provider or tier bindings | `providers/` |

A graph sits at the intersection and knows about neither.

## Autonomy is earned, per kind

Every write an agent can propose is a named **kind** carrying a **risk** and a
**ramp**. Kinds start propose-only. A kind graduates to auto-apply after N
consecutive clean outcomes at the human gate; one reversal resets the streak
and doubles the bar. Changing the cartridge or the model bindings resets
everything — a track record earned under different rules is not a track record.

The asymmetry is the argument: a wrong proposal costs a minute of review, a
wrong write costs an incident. Buy autonomy only where that ratio has been
measured.

## Status

Scaffold. `core/` modules carry their full contracts as docstrings and raise
`NotImplementedError` — they are written against, not ported. See
[`docs/CLEAN-ROOM.md`](docs/CLEAN-ROOM.md) for why, and for the rules on
fixtures and examples.

- [x] Base cartridge: roles, write kinds, routing, epic threshold, policy
- [x] Base context packs: conventions, epic model
- [x] Worked example team cartridge
- [x] Provider profile
- [ ] `core/` implementations
- [ ] Graphs
- [ ] Tests (synthetic fixtures only)

## Getting started

```bash
cp -r cartridges/example-team cartridges/my-team
$EDITOR cartridges/my-team/cartridge.yaml          # bind roles, name landings
cp context-templates/code-style.md cartridges/my-team/context/
$EDITOR cartridges/my-team/context/code-style.md   # in your own words
python -m core.cartridge --team my-team --json     # resolve + validate
```
