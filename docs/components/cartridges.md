# Cartridges

A cartridge is a packaged bundle of context for a team or a repo: its
conventions, its charter, its thresholds. Cartridges are what let the same
crew and the same graphs behave differently for different teams without
changing any code.

## What it owns

The `src/cartridges` folder holds the `core` package (merge, policy, ledger,
work store), the base and local cartridges, the provider profiles, the
reference skills plugins and the Claude plugin marketplace. See
[Cartridges](../methodology/cartridges.md) for how a run picks one.

## Installing it

Cartridges ships in every install. A workspace's `cartridges/base` and
`cartridges/local` link into the checkout's `src/cartridges/cartridges/`.

## Its own docs

The folder's README and docs live at
[src/cartridges](https://github.com/ppfenning/coxswain/tree/main/src/cartridges).
