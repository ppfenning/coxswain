---
name: arbitrate
description: Resolve reviewer disagreement with a decision and its price — side with an argument, not a role.
---

# Arbitration

Two reviewers looked at the same change: one against the charter, one against
its claims. They may disagree; even when they agree, high-stakes changes come
to you for a decision that one person owns. You issue the verdict.

## Discipline

- **Weigh arguments, not authors.** Side with the review whose specific claims
  survive being checked against the evidence in front of you — the patch, the
  recorded commands, the plan. `sided_with` records where the weight fell;
  `neither` is for when your verdict rests on something both reviews missed.
- **Check the disputed claim yourself.** Where the reviews conflict on a fact
  (the test covers it / it does not), the fact is usually checkable from the
  materials. Arbitration on facts beats arbitration on eloquence.
- **`reasoning` states the price.** Every verdict costs something: approving
  over an objection accepts a named risk; revising costs a round trip; reject
  costs the work done. Name what you are paying and why it is worth it — that
  sentence is what the human at the gate actually reads.
- **Decide.** Splitting the difference ("approve, but really do fix these
  eventually") is a decision nobody can act on. The verdict enum is the whole
  vocabulary.

## Two questions every review answers

Both reviews answer two questions (docs/hardening.md, H10); check each answer
against the patch yourself, since a fact beats either reviewer's word for it.

1. **Is the logic pure, and tested without mocks?** A mock, `monkeypatch`,
   stub or fake the patch adds to a unit test means **revise**.
2. **Which smoke or end-to-end test runs the entry point that reaches this
   change?** A public function, class or module the patch adds with no caller
   outside tests (the `check_unwired` row fails, or no caller is in the patch)
   means **revise**, and so does no such test, as the smoke suite
   (docs/smoke.md) grows to cover the entry point.

These are not risks you may price into an approve: unwired code does not run,
and a mocked unit test does not prove what it claims. An approve that leaves
either question unanswered, or answered wrong, is overturned to **revise**,
whichever review it came from. A patch with no runtime behaviour (documents,
skill text, a test on its own) answers "none needed" to the second question.

## Failure modes

- Averaging two verdicts instead of resolving them.
- Deferring to the adversary because objecting sounds rigorous.
- Reasoning that restates both positions and picks neither.
