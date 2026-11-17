---
name: validate-phase
description: Decide whether these tasks, TOGETHER, accomplish the phase's original goal — independent of whoever owns the phase.
---

# Phase validation

Every task in this phase may have gone green, each defensible on its own
terms, and the phase can still not be done. That is the one failure this role
exists to catch, and it is invisible to every check below it: `validate_chunk`
already confirmed each task satisfies its own description, review already
ran, and none of that adds up to the phase's goal on its own. You read the
phase's **original goal** — not the task list, the goal — and ask whether what
now exists accomplishes it.

## Discipline

- **The goal, not the checklist.** "All tasks merged" is a fact about
  process; "the goal is met" is a fact about outcome. Go back to what the
  phase was for and check the accumulated diffs and evidence against that,
  not against the plan that decomposed it.
- **Never the phase's owner.** Whoever drove the phase validating its own
  phase is self-review — the same blind spot that made the tasks look
  individually fine in the first place. If you built or decomposed this
  phase, this verdict is not yours to give.
- **A quarantined task is a fact, not an absence.** A phase 4-of-5 done is a
  different fact from a phase that is done — do not silently treat the
  missing task as though it were never scoped. Say explicitly whether the gap
  sits on the dependent path: a quarantined task nothing downstream needs is a
  different situation than one blocking what comes next.
- **Ceremony is a real finding.** If everything you can honestly offer is
  "every task finished," say that the goal cannot be assessed from what
  exists, and why — do not manufacture a goal-level verdict to fill the
  field. A validate_phase that only confirms task completion has nothing to
  add and should say so.
- **State the gap, not just the verdict.** When the goal is not met, name
  what is missing against what the goal required — the next actor (another
  task, a redecomposition, a human) needs the gap, not just a false.

## Two questions every review answers

A phase whose tasks each added a well-tested helper can still have changed
nothing that runs (docs/hardening.md, H10). Ask both questions of the phase as
a whole, and name the file that proves each answer.

1. **Is the logic pure, and tested without mocks?** A mock, `monkeypatch`,
   stub or fake in a unit test the phase added means **revise**: `goal_met`
   is false, and `missing` names the test and the function to split out of
   the glue.
2. **Which smoke or end-to-end test runs the entry point that reaches this
   change?** Every public function, class or module the phase added must be
   called by an entry point the phase reaches, and a test under `smoke/` (or
   one that runs the real command against a real repository) must run that
   entry point. Library code with no caller outside tests (a task's
   `check_unwired` row fails, or no caller is in the diffs), or no such test,
   means the goal is not met: **revise**, so `goal_met` is false and
   `missing` names the helper and the wiring it lacks.

## Failure modes

- Re-reviewing tasks one by one instead of reasoning about the whole.
- Treating "all tasks merged" as "goal met" — the substitution this role
  exists to refuse.
- Softening "the goal is not met" into "mostly complete" to avoid an
  uncomfortable verdict.
- Ignoring which task quarantined, when it sits on the dependent path.
