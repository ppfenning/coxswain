# Methodology

This page settles why the platform is shaped the way it is: work moves in
small tasks, review is blind by design, a claim needs the check that
proves it, and a budget is a shape constraint rather than a slider to turn
up.

## Small tasks

One ticket is one unit of work with one pull request. If a task needs
three PRs, it was really three tasks, and splitting it earlier is cheaper
than untangling it in review. A ticket that hasn't been scoped goes to a
future-work area, not onto the active board — scoping is what promotes
it.

## Blind reviewers

A charter reviewer checks a change against the team's written standards.
An adversary reviews the same change without seeing the charter
reviewer's verdict, looking for what the charter reviewer missed. Neither
sees the other's conclusion first, because a reviewer who already knows
"this passed" reviews differently than one who doesn't. See
[Review](review.md) for how the two verdicts are arbitrated into one.

## Evidence over assertion

A proposal that asserts something is true carries the check that proves
it: a command and its output, not a recollection. Absence of an error is
not evidence of success — a job that reports success while writing
nothing has not succeeded. See [Records](records.md) for what gets kept
as evidence, and where.

## Budgets as shape constraints

A budget is not a number to raise when a task runs over it. The rule is
split, never raise: a task that needs more than its ceiling allows was
too big for one task. See [Budgets](budgets.md) for the ceilings
themselves and what a stop records.

## The rest of this section

[Crew and seats](crew-and-seats.md) says who does the work.
[Cartridges](cartridges.md) says how a team's own standards get layered
in. [Graphs](graphs.md) says how a run is put together.
[Landing](landing.md) says how a run's change reaches a human.
