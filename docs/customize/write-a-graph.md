# Write a graph

This page settles the shape a new graph needs, where it goes, and how a
run picks it up. Anything the design hasn't named yet is marked
**planned** below rather than guessed at.

## The shape

A graph is the ordered set of nodes a run executes, with a DAG of real
dependencies between the tasks it schedules — see
[Graphs](../methodology/graphs.md) for what a node and an edge mean. A
new graph is a new course, not a new node: writing a new node type (a
new kind of review, a new kind of validation) is a different, larger
change than composing existing node types into a new order.

## Where it goes

**Planned.** The design has not yet named the directory layout or
naming convention a new graph file follows, or which repository owns it.

## How a run picks it up

**Planned.** The design does not yet say how a task or an epic selects
which graph runs it, beyond that a graph is "defined once, reused by
every run" — see [Start here](../start/index.md).

## What to check before writing a new one

A new graph is worth writing when an existing one's phases and edges
don't fit the work — not when a single task in the middle needs
different handling. A task that needs a different review tier or a
different budget is a [Policy](policy.md) change; a task that needs
different nodes entirely, in a different order, is a new graph.
