# Graphs

A graph is the ordered set of nodes a run executes: plan, build, review,
arbitrate, validate, and so on. It is defined once and reused by every run
that needs that shape of work.

## What it owns

The graphs repository owns the node definitions and the wiring between
them — which node hands off to which, and on what condition. See
[Graphs](../methodology/graphs.md) for the methodology behind the shape.

## Installing it

Graphs is required. It installs by default with `cox install`, no `--with`
flag needed.

## Its own docs

The graphs repository's own README and docs live at
[github.com/ppfenning/coxswain-graphs](https://github.com/ppfenning/coxswain-graphs).

## Reference

The component's README at the pinned tag is included below when the site is
built from a release.
