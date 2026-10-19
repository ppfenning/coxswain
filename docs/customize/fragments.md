# Fragments

This page settles what a fragment is, where it lives, and the rule that
governs one: `cartridge.d/*.yaml`, folded in sorted order, checked against
everything folded before it.

## cartridge.d/*.yaml

A fragment is a small YAML file that adds to one cartridge layer. It has
the same keys as `cartridge.yaml`. Fragments for a layer live beside that
layer's `cartridge.yaml`, in `cartridge.d/`, one concern per file:

```text
cartridges/my-team/
  cartridge.yaml
  cartridge.d/
    10-review.yaml
    20-checks.yaml
  context/
    charter.md
```

Prose (a charter, conventions) does not go in a fragment. It goes in a
markdown file that a `context:` list names. A fragment can add to that
list; its paths resolve from the team directory, not from `cartridge.d/`.

## Sorted order

Each layer folds its fragments in the sorted order of their file names,
after the layer's own `cartridge.yaml`. Base's fragments fold before base
joins local, and a team's fragments fold last. A later value wins a scalar
conflict, so the file name is part of the contract. Number the files when
order matters.

A fragment's `extends` key is ignored: the chain is fixed by each layer's
`cartridge.yaml` before any fragment is read. An empty fragment folds as
nothing.

## Tighten-only

Each fragment is checked against the accumulated authority at its point:
the parent chain, the layer's `cartridge.yaml`, and every fragment folded
before it. A fragment that lowers a write kind's risk or shortens its ramp
is refused, and the refusal names the fragment file. This is the rule in
[Cartridges](../methodology/cartridges.md#tighten-only), applied one file
at a time.

## Changing a fragment

Edit the file and commit it in your workspace's cartridges folder. Every
fragment's content is part of `cartridge_sha`, so changing one resets the
autonomy streaks the old rules earned. To see the result, resolve the
cartridge:

```sh
cartridge --team my-team --cartridges-dir <workspace>/cartridges --json
```
