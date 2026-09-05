# Review

This page settles how a change gets checked before it can land: a
charter reviewer holds it to the team's written standards, an adversary
is required to disagree, and the two verdicts are arbitrated by an
argument, not by whichever role spoke first.

## Charter review

The charter reviewer checks a change against the team's own written
standards — the style and correctness rules in its `code-style.md` or
equivalent pack. It is not reviewing taste; it is reviewing against a
document a human wrote and can point to. A charter nobody wrote is a
review that can only comment on taste, which is why the charter itself is
part of what a team customizes. See [Team
cartridge](../customize/team-cartridge.md).

## The adversary must disagree

The adversary reviews the same change without deferring to the charter
reviewer's verdict, and its job is specifically to find what the charter
reviewer missed. An adversary that rubber-stamps whatever the charter
review already said is not doing the job the seat exists for — the two
reviews are only useful apart if they were actually produced
independently.

## Arbitration sides with an argument, not a role

When the charter reviewer and the adversary disagree, the arbiter reads
both verdicts and sides with whichever one has the stronger argument, not
with whichever role usually wins. Neither reviewer's seat carries
authority by default; the reasoning does.

## validate_chunk and validate_phase are blind by design

`validate_chunk` checks one task's result against the checks the project
actually runs, and `validate_phase` checks a phase's tickets together the
same way — both run without seeing the charter or adversary verdicts
first. This is blind on purpose: a validator that already knows the
reviewers approved a change validates differently than one that hasn't
been told, and the point of validation is that it runs the checks itself
rather than trusting what review already said. Success reported by
review is not evidence — a validator that trusted a status field would
confidently report a run that wrote nothing.

## What follows review

A verdict that survives arbitration and validation is what reaches
[Landing](landing.md). A verdict that doesn't goes back to build, in the
same worktree, for the same task.
