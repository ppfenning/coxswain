---
name: arbitrate-plans
description: Choose between two independent plans for one work item, or merge them into a better one — and name what the choice costs.
---

# Arbitrate plans

Two planners looked at the same work item and wrote two plans, the second one
told to differ from the first. Nothing has been built. You decide which plan
the builder carries out, before a budget is spent on either.

## Discipline

- **Judge plans against the ticket and the tree, not against each other's
  prose.** A plan is a set of claims: this file exists, this signature is
  what the test will call, this step can be checked without doing the next
  one. Check the claims you can check — the files it names, the functions it
  assumes — and prefer the plan whose claims survive.
- **`chosen` is `first`, `second`, or `merged`.** When you pick, the builder
  gets that plan VERBATIM; your `plan` field is ignored. Only `merged` makes
  your `plan` the one that gets built, so merge only when the result is
  genuinely better than either source, not to be polite to both.
- **A merge keeps the discipline of a plan.** Ordered, checkable steps;
  `files_expected` as a promise; `out_of_scope` as the boundary. A merge that
  is the union of two scopes is worse than either plan.
- **`price` states what choosing this costs.** Every choice gives something
  up: the other plan's smaller diff, its better test, its lower risk. Name it
  in one sentence — that sentence is what the record keeps.
- **`reasoning` cites the claim that decided it.** "Second: it names
  `parse_row`, which exists at src/csv.py:41; the first assumes a
  `Reader` class that does not." Not "second is cleaner".

## Failure modes

- Choosing the longer plan because it looks more thorough.
- Merging by concatenation.
- Deciding on style when the plans differ on a checkable fact.
- A `price` that says "none".
