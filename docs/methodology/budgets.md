# Budgets

This page settles what a budget is for: a per-node dollar ceiling by
tier, enforced by one rule — split, never raise — and a record of exactly
what happened when a task hit its ceiling.

## Ceilings are per node, by tier

Every node in a graph — plan, build, review, and the rest — has a dollar
ceiling for the provider tier it runs on. A cheap tier gets a smaller
ceiling; a tier used for harder work gets more room, but still a fixed
one. The ceiling is a property of the node and the tier, not something a
single run negotiates for itself.

## Split, never raise

When a task is going to exceed its ceiling, the answer is not a bigger
number. It is a smaller task. A task that needs more budget than its
tier allows was too large for one task, the same way a ticket that needs
three pull requests was really three tickets. Raising the ceiling to fit
the task hides the signal that the task should have been split before it
ever ran.

## budget_usd on an item

A work item can carry its own `budget_usd`, narrower than the tier
ceiling if the team wants a tighter cap on a specific kind of task. This
follows the same tighten-only rule cartridges use elsewhere: an item's
budget can only be smaller than what its tier allows, never larger. See
[Cartridges](cartridges.md) for tighten-only in general and
[Policy](../customize/policy.md) for `build_budget_usd_max`, the knob a
team sets to change the ceiling itself.

## What a budget stop records

When a node hits its ceiling, the run stops there rather than continuing
on credit. What gets recorded is the amount spent against the ceiling,
the node that was running, and the task it was working — enough for a
human to see whether the task needs splitting or the ceiling needs
revisiting for that tier generally, as a policy change, not as an
exception for one run.
