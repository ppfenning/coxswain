# Dash

Dash is coxtop, a terminal dashboard for the fleet: lanes, the chair, the
queue and spend on one screen, with a drill-down into any run. It reads
`cox dash --feed`, so it shows what the store shows.

## What it owns

The dash repository owns the `coxtop` binary (Rust and ratatui), its themes
and its keymap. Every action it offers is a `cox` verb; it holds no state of
its own beyond the view you left it on.

## Installing it

Dash is optional: nothing else needs it. It joined the release in 0.26.0 as
a beta preview and is tagged in lockstep with the rest. Install it with
cargo, at the tag of your Coxswain release:

```sh
cargo install --git https://github.com/ppfenning/coxswain-dash --tag <release tag> coxtop
```

Prebuilt binaries and a tap formula are planned, so cargo will not always be
needed.

## Its own docs

The dash repository's own README lives at
[github.com/ppfenning/coxswain-dash](https://github.com/ppfenning/coxswain-dash).

## Reference

The component's README at the pinned tag is included below when the site is
built from a release.
