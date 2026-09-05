# Landing

This page settles how a run's change reaches a human: every governance
path ends the same way, on a branch, as a pull request, merged only on
green.

## Every path ends on a branch

A run's work happens in a worktree that belongs to that run alone. What
leaves the worktree is a diff, applied to a scratch branch — never
committed by the run directly onto a branch a human or another run is
also using. The scratch branch is disposable in the same way the
worktree is: a run that produced something wrong costs nothing to throw
away before a human ever sees it.

## Phase branches collect a phase's tasks

Where a graph's tasks belong to an ordered phase, their scratch branches
land on a shared phase branch rather than each opening its own pull
request against the epic's target branch. This keeps the phase reviewable
as one unit instead of as however many tasks happened to be in flight.

## Pull requests are where a human decides

From a branch, the change becomes a pull request. Nothing in the loop
merges it. A human, or CI, or both, decide whether it merges — the run's
job ends at handing over a reviewable diff with its evidence attached,
not at getting the diff into the target branch.

## Merge on green

A pull request merges once its checks are green, not once a run has
asserted that they are. This is the same evidence-over-assertion rule the
review nodes already enforce, applied at the last step instead of the
review steps: the checks that actually ran are what green means.

## runs clean

`runs clean` removes the worktrees and scratch branches left behind by
runs that finished — landed, abandoned, or superseded — so a workspace
doesn't accumulate disposable state that nobody is going to look at
again. It never touches a run still in flight or a branch a pull request
still points at.
