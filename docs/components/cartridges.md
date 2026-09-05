# Cartridges

A cartridge is a packaged bundle of context for a team or a repo: its
conventions, its charter, its thresholds. Cartridges are what let the same
crew and the same graphs behave differently for different teams without
changing any code.

## What it owns

The cartridge repository owns the fragments a seat's prompt is assembled
from, and the top-level cartridge files — `conventions.md`, `charter.md`,
`epic-model.md`, `prose-style.md` — that every run in this substrate loads.
See [Cartridges](../methodology/cartridges.md) for how a run picks one.

## Installing it

Cartridges is required. It installs by default with `cox install`, no
`--with` flag needed.

## Its own docs

The cartridges repository's own README and docs live at
[github.com/ppfenning/coxswain-cartridges](https://github.com/ppfenning/coxswain-cartridges).

## Reference

The component's README at the pinned tag is included below when the site is
built from a release.
