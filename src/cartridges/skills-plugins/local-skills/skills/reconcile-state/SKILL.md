---
name: reconcile-state
description: Given declared state and actual state, name each drift and the smallest correction that closes it.
---

# Reconcile

An epic declares what should be true — which tickets exist, what state each is
in. The board says what is true. You receive both, plus a deterministic
set-comparison already computed, and produce drift entries with corrections.
You read nothing yourself: both states arrive as arguments, because a
reconciler that fetches cannot be replayed.

## Discipline

- **The set arithmetic is done; judgment is why you are here.** Missing and
  extra tickets are computed before you run. Your work is the drifts that need
  reading: a ticket marked Done whose acceptance criteria are visibly unmet, a
  state that is *ahead* of reality versus one that is merely stale.
- **Correction is the smallest closing move.** `state_move` when the item is
  right and its state is wrong; `item_update` when the item's content has
  drifted; `none` when the drift is real but correcting it is not this
  system's call — with `detail` saying whose it is.
- **Direction matters.** Declared-ahead-of-actual (claims Done, is not) is a
  worse drift than actual-ahead-of-declared (done, not yet marked). Say which
  way each drift points; a human scanning the summary triages on that.
- **Do not launder disagreement into drift.** If declared and actual disagree
  because the declaration was wrong when written, the correction is to the
  declaration — flag it, do not "fix" the board to match a bad plan.

## Failure modes

- Re-deriving the set comparison and getting it differently.
- `state_move` for everything — some drift is content, not state.
- A summary that counts drifts without saying whether any of them matter.
