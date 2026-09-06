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
- **Every phase states one goal line.** The phase validator judges the
  phase's work against that line, so it names the outcome, not the tasks.
- **Tasks are one-sitting sized.** Each task's `body` should let a builder
  start without re-reading the initiative: what to do, what done looks like,
  what is out of bounds. A task needing its own decomposition was scoped too
  big.
- **A task's tests ride with its code.** Never split tests into a task of
  their own — a separate tests task doubles the reviews and lands code
  without its evidence.
- **`surfaces` are paths, not prose.** Each entry is a file path in the
  checkout — one that already exists, or one marked `(new)` under a
  directory that already exists — never a description of an area. That
  field decides how hard the work is reviewed later; an empty surfaces list
  on a dangerous task under-reviews it.
- **A title is plain text.** A backtick in YAML frontmatter breaks the file,
  so titles carry none.
- **A task whose surfaces live in another repository is that repository's
  task.** Link it with `blocked_on` instead of folding its work in here.
- **Ids are stable slugs.** They become filenames and dependency references;
  rename one later and every edge naming it dangles.
- **Tasks in one phase never share a file.** Every task names the files it
  will create or edit in `surfaces`, in the terms the team's review policy
  uses, and again in its body, and no file appears under two tasks of the
  same phase, whether or not one `needs` the other — a `needs` edge orders
  the work but the tasks still merge into the phase branch afterwards. If
  two pieces of work need the same file they are one task, or the second is
  in a later phase and `needs` the first.
- **Done-criteria are facts the applied patch and the named checks can
  produce.** The record a task is judged against is the diff plus the output
  of the checks the cartridge names, nothing outside it. A body that asks for
  an ad hoc command's output, a diff against another branch, a read of
  another repository, or a run from before the patch existed is asking for
  evidence that never enters that record. State the fact in the body
  instead, or make it a reviewer's own check.

- **A ticket body carrying `split recommended` is two tasks.** The harness
  writes it when a build ran out of budget twice with partial work, and lists
  the files done and the surfaces untouched. That list is the seam: the done
  files become one task, the untouched surfaces another, and the second
  `needs` the first when it consumes what the first produced.

## Failure modes

- A linear chain — everything needs the previous thing. Almost always a sign
  edges were written from narrative order, not data flow.
- A fully parallel plan — nothing needs anything. The opposite lie.
- Tasks whose bodies say "see initiative".
- Two tasks in one phase that both create or edit the same file — the runner
  merges phase tasks in parallel, so the second write is a guaranteed merge
  conflict, and a `needs` edge between them does not prevent it.
- A done-criterion that names evidence outside the diff and the named
  checks — nobody downstream can produce it, so the task can never close.
