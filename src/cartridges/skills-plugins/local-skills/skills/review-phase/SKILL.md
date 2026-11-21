---
name: review-phase
description: Review a whole phase branch against its base — does it meet the phase's goal, and does it carry fat?
---

# Phase review

The phase's tasks were built and checked one by one, and rolled up onto the
phase branch without a review each. The phase smoke has just passed at the
branch's tip. You review the phase once, as a whole: the diff in front of you
is the branch against its base, every task and every earlier revision
together, and the smoke's checks are quoted as the harness ran them.

You answer two questions, and name the file that proves each answer.

## 1. Does the phase meet its goal?

- **The goal, not the checklist.** Read the phase's goal and ask whether what
  now exists does it. Tasks that each passed their own checks can still not add
  up, and "every task finished" is a fact about process, not the goal being met.
- **Reached, not only written.** A helper no entry point of the phase calls, or
  a command the phase added that nothing runs, does not meet a goal. The smoke
  passing says the wiring the smoke covers holds; it says nothing about what it
  does not run.
- **State the gap.** When the goal is missed, say what is missing against what
  the goal asked for, in the file where it belongs.

## 2. Does it carry fat?

Fat is code the goal does not need. Each of these is a finding with its file:

- a helper, constant or block written twice, often by two tasks built in
  parallel that each needed it;
- a function, class or module nothing the phase reaches calls;
- a layer, wrapper or parameter that only passes a value through;
- a test of nothing the phase changed, or a test that only restates a mock.

Fat is not style. A name you would have chosen differently, a docstring you
would have cut, an order of arguments: note it in the rationale, never as a
finding.

## Verdicts

- `approve`: the goal is met and there is no fat.
- `revise`: one revision of the branch fixes every finding. The findings ARE the
  revision task another agent will build, so each says exactly what to change,
  in which file: "delete lib/copy.py and import helper from lib/helper.py in
  lib/notes.py", not "deduplicate the helpers". Ask for nothing the two
  questions did not find.
- `reject`: the goal is missed in a way one revision cannot fix: the phase was
  decomposed wrong, or what it built is the wrong thing. Your rationale is the
  reason the phase is quarantined and what triage reads first, so write it for
  that reader.

A later round sees what the earlier rounds said. Judge whether their revisions
did what was asked; do not ask again for what is already done, and do not raise
a new finding in round three that was there in round one.
