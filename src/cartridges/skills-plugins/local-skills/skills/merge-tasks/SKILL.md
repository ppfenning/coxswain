---
name: merge-tasks
description: Merge two parallel tasks that edited the same code, keeping what both meant — or say plainly that they cannot both hold.
---

# Merge tasks

Two tasks of one phase were built in parallel, each on the phase tip of its
moment. The first is already on the phase branch. The second's change does not
apply on top of it, because both edited the same lines. You are handed both
tasks' titles, the second task's whole change, and every conflicted file as a
three-way apply left it: the phase branch's side between `<<<<<<<` and
`=======`, the second task's between `=======` and `>>>>>>>`.

## Your job

For each conflicted file, return its whole merged text, keeping what BOTH
tasks meant. Usually that is both edits side by side: two new branches of one
function, two new arguments, two new entries in one list. Read the code around
the markers before you choose an order; the merged text has to be correct code,
not two hunks stacked.

## Must not

- Change anything outside what the two tasks changed. No cleanup, no renames,
  no reformatting of lines neither task touched.
- Drop one side to make the conflict go away. Losing a task's edit is not a
  merge, even when the result runs.
- Leave a conflict marker in, or return a file that was not conflicted.

## When they cannot both hold

When the two edits contradict each other (one removes what the other extends,
both rename one thing two ways), answer `resolved: false` and say in one line
why. That is a correct answer, not a failure: the harness then builds the
second task again on top of the first.

## What happens next

You are not the last word. The harness runs both tasks' cheap checks on your
merge; if either fails, your merge is thrown away and the second task is
rebuilt. The collision is recorded either way, as evidence that the phase's
decomposition missed a dependency.

## Output

`resolved`, the `files` (each `path` and its whole merged `content`), and a
one-line `summary` of how the two edits were combined, or why they could not be.
