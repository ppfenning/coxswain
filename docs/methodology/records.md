# Records

This page settles what a run leaves behind: a log, a trace for every
node it executed, a usage record, a ledger a human reads to decide
whether to trust the result, and the work store's own account of how
many times a task was attempted.

## The run record

Every run keeps a log of what it did in order, and a trace per node — one
record per plan, build, review, arbitrate, and validate step the run
actually executed, not a summary written after the fact. The trace is
what lets a human, or the next node, reconstruct what a given seat saw
and returned, rather than trusting a paraphrase of it.

## usage.json

A run's cost is not left to be reconstructed from a provider's own
billing later. Each run writes a usage record — the tokens and dollars
spent per node — so a budget stop (see [Budgets](budgets.md)) or a
retrospective on cost can be checked against a file instead of a memory
of what the run probably cost.

## The ledger

The ledger is the record of what a run did: the evidence, the verdicts,
and the patch. It's what a human reads to decide whether to trust the
result — not the run's own summary of itself, but the artifacts a
summary would otherwise just be describing.

## attempts, in the work store

The work store tracks, per task, how many times it has been attempted —
its `attempts` count — independent of any one run's own record. A task
that has failed validation twice looks different to the docket than one
seeing its first attempt, even though both look identical from inside a
single run's own trace.
