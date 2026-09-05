# Tools

Tools is the `cox` CLI itself: the command that installs the rest of a
Coxswain release, dispatches work to the crew, and drives the loop from
intake to landing on a branch.

## What it owns

The tools repository owns the `cox` command, its subcommands (`cox
install`, `cox release`, and the rest), and the dispatcher that enforces the
in-flight bound.

## Installing it

Tools is required and provides the `cox` binary. It installs by default
with `cox install`, no `--with` flag needed — everything else is installed
through it.

## Its own docs

The tools repository's own README and docs live at
[github.com/ppfenning/coxswain-tools](https://github.com/ppfenning/coxswain-tools).

## Reference

The component's README at the pinned tag is included below when the site is
built from a release.
