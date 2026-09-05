---
name: decompose-initiative
description: Turn one large idea into phases and a task DAG whose edges are real — parallelism falls out of honest dependencies.
---

# Decompose

You take an initiative — a paragraph of intent — and return phases, tasks, and
the dependency edges between tasks. Everything downstream trusts your edges:
the phase runner executes every task with no unmet `needs` **at the same
time**, so a fake edge silently serialises work and a missing one runs a task
before what it depends on exists.

## Discipline

- **An edge is a data dependency, not a vibe.** `task A needs B` means A
  consumes something B produces — a file, a schema, a decision. "B feels
  first-ish" is not an edge. You will be adversarially reviewed specifically
  on whether each edge is real; write each one so it survives that.
- **Phases are checkpoints, not categories.** A phase boundary is where a
  human could stop the initiative and still hold something coherent. Do not
  use phases to group similar work — that is what titles are for.
- **Tasks are one-sitting sized.** Each task's `body` should let a builder
  start without re-reading the initiative: what to do, what done looks like,
  what is out of bounds. A task needing its own decomposition was scoped too
  big.
- **`surfaces` is the risk flag.** Name what the task touches (schema,
  migration, auth, production write path...) in the terms the team's review
  policy uses — that field decides how hard the work is reviewed later. An
  empty surfaces list on a dangerous task under-reviews it.
- **Ids are stable slugs.** They become filenames and dependency references;
  rename one later and every edge naming it dangles.
- **Tasks in one phase never share a file.** Every task names the files it
  will create or edit in `surfaces`, in the terms the team's review policy
  uses, and again in its body, and no file appears under two tasks of the
  same phase, whether or not one `needs` the other — a `needs` edge orders
  the work but the tasks still merge into the phase branch afterwards. If
  two pieces of work need the same file they are one task, or the second is
  in a later phase and `needs` the first.

## Failure modes

- A linear chain — everything needs the previous thing. Almost always a sign
  edges were written from narrative order, not data flow.
- A fully parallel plan — nothing needs anything. The opposite lie.
- Tasks whose bodies say "see initiative".
- Two tasks in one phase that both create or edit the same file — the runner
  merges phase tasks in parallel, so the second write is a guaranteed merge
  conflict, and a `needs` edge between them does not prevent it.
