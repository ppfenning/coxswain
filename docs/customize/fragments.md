# Fragments

This page settles what a fragment is, where it lives, and the one rule
that governs editing one: `cartridge.d/*.yaml`, read in sorted order,
tighten-only, and never edited in place once it's shipped — the editor
writes a new `edited.yaml` instead.

## cartridge.d/*.yaml

A fragment is one piece of context text — a convention, a charter
section, a style note — that gets assembled into a seat's prompt for a
run. Fragments for a layer live as separate files under that layer's
`cartridge.d/`, one concern per file, rather than one large file covering
everything the layer wants to say.

## Sorted order

Fragments assemble in the sorted order of their file names. This makes
the file name part of the contract: two fragments that must appear in a
particular order in the assembled prompt need file names that sort that
way, and renaming a fragment changes where it lands.

## Tighten-only

A fragment can only narrow what a lower layer already set — raise a
threshold, add a required check, shrink a budget — never loosen it. This
is the same rule described in [Cartridges](../methodology/cartridges.md),
applied at the level of one file instead of one layer.

## edited.yaml

A fragment isn't hand-edited in its shipped location. An editor working
on a fragment writes its changes to `edited.yaml` alongside it, so the
original fragment and the proposed change are both on disk and diffable
against each other, rather than a single file being overwritten in place
with no record of what it said before.
