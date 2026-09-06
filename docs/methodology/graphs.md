# Graphs

This page settles what a run actually executes: a graph is a course laid
out in advance — phases, tasks, and a DAG of edges that are real
dependencies, not just the order someone happened to write them down.

## A graph is a course

A graph is the ordered set of nodes a run executes: plan, build, review,
arbitrate, validate, and so on. It is defined once and reused by every
run, the way a rowing course is laid out once and rowed by every crew
that races it.

## What a graph may not do

A graph owns sequence. It decides which node runs next and what each one
is asked. It writes nothing at all: no file, no branch, no pull request,
no ledger row.

Every consequence belongs to the harness instead — the worktree a build
runs in, the checks that have to pass, the gate a write goes through, and
the append-only ledger that records what happened.

The split is what makes a new graph cheap. Write one and it inherits the
gate, the worktree, the checks and the ledger by existing, rather than by
remembering to call them. It also bounds what a bad graph can cost you:
the worst it can do is ask for the wrong thing in the wrong order,
because it has no way to touch the repository at all.

Two more names finish the picture. A **cartridge** says who a run works
for — which skills a role uses, which model a tier gets, where a write is
allowed to land. A **runner** executes one node against a provider and
returns what it said. Swap the runner and the same graph runs against a
different provider; swap the cartridge and the same graph works for a
different team.

## Phases order tickets that need it, and only those

An epic is a container with one section per phase. Phases are ordered;
tickets within a phase are not necessarily. A dependency edge belongs
between two tickets only where order genuinely matters — a dependency
that exists because a task "feels sequential" blocks work that could have
run in parallel for no reason.

## Tasks are the DAG's nodes

A task is one unit of work: one ticket, one pull request. The DAG's edges
run between tasks, and an edge that isn't a real dependency shouldn't
exist — it costs the docket a slot that could have dispatched something
else.

## Run ids and worktrees

Every execution of a graph against a task gets a run id, so the record
for one attempt never gets confused with another attempt at the same
task. Each task gets its own worktree: a disposable checkout that one run
owns and nothing else touches, so a run that goes wrong costs nothing to
throw away. See [Records](records.md) for what a run id ties together.

## How to read the diagrams

Each graph's page below embeds two things pulled from the graphs
repository at build time: a mermaid diagram of the nodes and edges, and a
table listing every node with what it does. A solid edge is an
unconditional handoff. A labeled edge is a branch the node before it
decides. The diagram is generated from the graph's own definition, so it
never drifts from what actually runs.

## The seven graphs

| Graph | What it does |
| --- | --- |
| [coxswain](graphs/coxswain.md) | The dispatch loop: reads the docket and starts the graph each task needs. |
| [epic-swarm](graphs/epic-swarm.md) | Fans an epic out into its tickets and runs them against the phase order. |
| [lifecycle-propose](graphs/lifecycle-propose.md) | Walks one ticket through its states, proposing each transition. |
| [initiative-decompose](graphs/initiative-decompose.md) | Breaks an initiative into epics and tickets during scoping. |
| [epic-reconcile](graphs/epic-reconcile.md) | Reconciles an epic's board state against what its tickets' records show. |
| [retro-propose](graphs/retro-propose.md) | Drafts a retro from an epic's run records for a human to accept. |
| [triage-propose](graphs/triage-propose.md) | Proposes where an unscoped ticket belongs before it is planned. |

## Where a graph's shape is decided

The nodes and edges of a graph are the platform's own concern, not a
team's. A team customizes what runs inside a node — which skills a seat
uses, which charter a reviewer holds work to — without changing the graph
itself. See [Write a graph](../customize/write-a-graph.md) for what is
and isn't customizable here today.
