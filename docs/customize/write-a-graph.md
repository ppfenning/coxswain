# Write a graph

This page settles the shape a new graph needs, where it goes, and how a
run picks it up.

## The shape

A graph is the ordered set of nodes a run executes, with a DAG of real
dependencies between the tasks it schedules — see
[Graphs](../methodology/graphs.md) for what a node and an edge mean. A
new graph is a new course, not a new node: writing a new node type (a
new kind of review, a new kind of validation) is a different, larger
change than composing existing node types into a new order.

In code, a graph is a module with a function `run(args, runner) -> dict`.
It reads its cartridge and date from `args`, asks the runner for roles
and tiers, never skills or models, and returns proposals as data. It
writes nothing. `src/graphs/docs/GRAPH-CONTRACT.md` lists the rules,
and `tests/test_portability.py` in `src/graphs` enforces them.

## Where it goes

A graph is a module under `src/graphs/graphs/delivery/` (work that
produces work) or `src/graphs/graphs/ops/` (keeping a running system
honest). The module opens with a docstring that names the node chain
right under its title line, such as `decompose -> adversary -> emit`; the
diagram on the
graph's docs page is generated from it by `python -m harness.graphdoc`.
The module declares `GRAPH_NAME` and a module-level `SPEC`, a
`graphs._spec.GraphSpec` naming the subcommand, the run function, a
one-line summary and the `Need`s that become command-line flags.

Graphs are platform code. They live in the coxswain repository, not in a
team cartridge, and a new one lands as a pull request there.

## How a run picks it up

Discovery is automatic. `harness/registry.py` imports every module under
`graphs/` and collects each `SPEC`, so a new module is a new
`shell.py <subcommand>` with no dispatch table to edit. Two names
clashing is an error, not a silent shadow.

Reaching it from `cox` is not automatic:

- `cox route launch` starts a fixed set: `epic`, `decompose`, `rescue`,
  `cos` and `sweep`.
- The coxswain dispatch graph can select only graphs its driver,
  `harness/cos.py`, knows how to build arguments for (`_KNOWN_GRAPHS`).
  A new graph joins dispatch by extending that list and its argument
  builder.

## What to check before writing a new one

A new graph is worth writing when an existing one's phases and edges
don't fit the work — not when a single task in the middle needs
different handling. A task that needs a different review tier or a
different budget is a [Policy](policy.md) change; a task that needs
different nodes entirely, in a different order, is a new graph.
