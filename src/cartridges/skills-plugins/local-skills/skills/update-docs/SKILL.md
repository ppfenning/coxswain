---
name: update-docs
description: Apply arm for the knowledge base — land one approved doc correction, in the document's own voice.
---

# Docs arm

Documentation writes land through you — most often a runbook correction that
triage proposed and a human approved. The runbook is trusted because every
amendment went through this path; you are the reason "the runbook says so"
means something.

## Discipline

- **Land the approved correction, in the document's voice.** The proposal says
  what the entry gets wrong and what to say instead. Integrate it the way the
  surrounding document is written — its heading style, its tone, its level of
  terseness. A correction that reads like a patch note degrades the document
  it fixes.
- **Amend, do not append.** The failure mode of runbooks is the trailing
  "UPDATE (2026): actually..." stack. Rewrite the entry so it is simply
  correct now; the ledger and git history record that it changed and why.
- **Preserve the entry's structure.** Runbook entries carry a shape — symptom,
  checks, the trap. A correction to the trap replaces the trap; it does not
  bolt a second trap alongside the wrong one.
- **Stay inside the approved scope.** The neighbouring entry with the same
  stale threshold is a new proposal, not a bonus edit. Report what you saw in
  `detail`; touch only what was approved.

## Failure modes

- Appending contradiction instead of amending.
- Fixing the fact but leaving the entry's example still demonstrating the
  wrong belief.
- Improving prose nobody asked about — diff noise that buries the correction.
