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

Each graph's page below embeds what the build pulls from `src/graphs`: a
mermaid diagram of the nodes and edges, a table listing every node, and,
for most graphs, the graph's own prose page. A solid edge is an
unconditional handoff. A labeled edge is a branch the node before it
decides. The diagram is generated from the graph module's docstring by
`python -m harness.graphdoc`, so it changes when the graph does.

## The eleven graphs

The registry is whatever `harness/registry.py` discovers: every module
under `graphs/` that declares a `SPEC`. Today that is eleven graphs. The
subcommand is what `shell.py` takes; the graph name is what the run
records.

| Subcommand | Graph | What it does |
| --- | --- | --- |
| `decompose` | [initiative-decompose](graphs/initiative-decompose.md) | Turns an idea into phases and a task DAG, attacks the dependency edges, and proposes one work item per task. |
| `lifecycle` | [lifecycle-propose](graphs/lifecycle-propose.md) | The development loop for one task: scope, plan, build in a worktree, review, adversary, arbitrate. |
| `validate` | [phase-validate](graphs/phase-validate.md) | Judges whether a phase's tasks, together, met the phase's goal. Advisory; emits no proposals. |
| `review-diff` | [review-diff](graphs/review-diff.md) | Reviews an arbitrary diff outside any build's own run: verdict and findings out, nothing written. |
| `rescue-review` | [rescue-review](graphs/rescue-review.md) | Reviews a patch that already exists against the harness's own check results. |
| `cos` | [coxswain](graphs/coxswain.md) | One dispatch node: picks which graphs to run next from a docket the driver assembled. |
| `reconcile` | [epic-reconcile](graphs/epic-reconcile.md) | Compares an epic's declared state with its actual state and proposes corrections. |
| `retro` | [retro-propose](graphs/retro-propose.md) | Reads ledger rows back and proposes only doc updates it can cite. |
| `triage` | [triage-propose](graphs/triage-propose.md) | Morning triage of an alert queue: read-only, proposes runbook corrections. |
| `triage-quarantine` | [triage](graphs/triage.md) | Classifies a task stranded at its attempt cap and emits a write by class. |
| `sweep` | [sweep](graphs/sweep.md) | Applies one rule everywhere it matches: plan, apply, verify, review. |

`delivery` holds the first five, `ops` the other six.

## Drivers are not graphs

`shell.py` also takes three subcommands that are not graphs. They invoke
graphs, block on them and write, which a graph may not do.

- `phase` runs `lifecycle` once per unblocked task of one phase.
- `epic` is [the epic driver](graphs/epic-driver.md). `cox route launch epic`
  starts it. It walks an initiative phase by phase: `lifecycle` per ready
  task, `validate` per phase, `triage-quarantine` for a task at its attempt
  cap.
- `rescue` runs `rescue-review` on one quarantined task's kept patch.
  `cox route launch rescue` starts it.

`cox route launch` also starts `decompose`, `cos` and `sweep` directly.
`cox runs review --pr <url>` runs `review-diff` on a pull request.

## Where a graph's shape is decided

The nodes and edges of a graph are the platform's own concern, not a
team's. A team customizes what runs inside a node — which skills a seat
uses, which charter a reviewer holds work to — without changing the graph
itself. See [Write a graph](../customize/write-a-graph.md) for what is
and isn't customizable here today.
