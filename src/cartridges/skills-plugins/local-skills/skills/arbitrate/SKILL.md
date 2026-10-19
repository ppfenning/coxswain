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

## Failure modes

- Averaging two verdicts instead of resolving them.
- Deferring to the adversary because objecting sounds rigorous.
- Reasoning that restates both positions and picks neither.
