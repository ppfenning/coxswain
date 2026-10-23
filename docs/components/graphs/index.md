# Graphs

A graph is the ordered set of nodes a run executes: plan, build, review,
arbitrate, validate, and so on. It is defined once and reused by every run
that needs that shape of work.

## What it owns

The `src/graphs` folder holds the graphs, the harness that runs them (the
gate, the budget, the checks, the records), and the runners that execute
nodes. Eleven graphs register a subcommand: `decompose`, `lifecycle`,
`validate`, `review-diff` and `rescue-review` for delivery, and `cos`,
`reconcile`, `retro`, `triage`, `triage-quarantine` and `sweep` for
operations. The epic driver, which runs an initiative phase by phase, is
harness code, not a graph. See [Graphs](../../methodology/graphs.md) for the methodology behind the
shape.

## Installing it

Graphs ships in every install. The profile's `harness_dir` points at
`<checkout>/src/graphs`, and the harness runs in the checkout's venv.

## Its own docs

The folder's README and docs live at
[src/graphs](https://github.com/ppfenning/coxswain/tree/main/src/graphs).
