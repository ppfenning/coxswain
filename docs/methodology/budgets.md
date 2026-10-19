# Budgets

This page settles what a budget is for: a per-node dollar ceiling by
tier, enforced by one rule — split, never raise — and a record of exactly
what happened when a task hit its ceiling.

## Ceilings are per node, by tier

Every node in a graph (plan, build, review and the rest) has a dollar
ceiling for its session. The provider profile sets it: `budget_usd` per
tier (the Claude Code profile: cheap $0.15, standard $0.35, deep $0.80),
and `role_budget_usd` for roles that need a different number, such as the
reviewers. The ceiling is a property of the node and the tier, not
something a single run negotiates for itself.

A build is the exception that proves the rule. Its budget is a guide for
one slice, not a ceiling for the task. The guide is estimated from the
repository's recent builds, clamped to $1.00 to $4.00. At the guide the
harness judges go or no-go; a build that passes resumes. The provider
profile's `role_ceiling_usd` (the Claude Code profile: build $6) is the
failsafe on the build's cumulative spend. See
[Autonomous chair](../customize/autonomous-chair.md#build-checkpoints).

## Split, never raise

When a task is going to exceed its ceiling, the answer is not a bigger
number. It is a smaller task. A task that needs more budget than its
tier allows was too large for one task, the same way a ticket that needs
three pull requests was really three tickets. Raising the ceiling to fit
the task hides the signal that the task should have been split before it
ever ran.

## budget_usd on an item

A work item can carry its own `budget_usd`. It replaces the estimated
build guide for that item. It may be higher than the estimate, but never
higher than the cartridge's `build_budget_usd_max` (base: $6). The
harness refuses an item that asks for more and quotes the cap. See
[Policy](../customize/policy.md#build_budget_usd_max).

## What a budget stop records

When a node hits its ceiling, the run stops there rather than continuing
on credit. What gets recorded is the amount spent against the ceiling,
the node that was running, and the task it was working — enough for a
human to see whether the task needs splitting or the ceiling needs
revisiting for that tier generally, as a policy change, not as an
exception for one run.
