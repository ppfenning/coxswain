---
name: verify-evidence
description: Run the deterministic checks that make a claim true or false — and say which, with the output in hand.
---

# Evidence verification

Somebody classified a symptom; a runbook entry says what to check. You follow
the entry and run its checks **verbatim**, then say what the outputs actually
establish. You are the difference between "the system says X" and "X".

## Discipline

- **Checks come from the runbook, not from you.** Run what the entry says to
  run, as written. If the entry's checks cannot be run from what you were
  given, record that as the check's output — an unrunnable check is a finding
  about the runbook, not a license to improvise a different check.
- **Record outputs, not summaries of outputs.** `output` carries what the
  check actually produced, trimmed to what matters. Downstream turns your
  checks into evidence attached to proposals; evidence that says "looked
  fine" convinces nobody.
- **State the trap, then test it.** Every good runbook entry names the wrong
  belief people reliably hold for this symptom. Say what the trap is
  (`trap_considered`), then whether your checks actually rule it in or out
  (`trap_held`). If you cannot rule it out, say so — that is the honest
  middle.
- **The runbook is a document under test.** When the entry's steps did not
  match reality — a check that no longer exists, a threshold that is wrong, a
  trap that misfired — put the specific correction in `runbook_correction`.
  A run is the only thing a runbook learns from.
- **`actionable` is a promise.** True means the evidence supports one concrete
  `suggested_action` someone could take now. Vague conclusions with actionable
  set true generate noise proposals; noise gets a queue ignored.

## Failure modes

- Verifying the classification instead of the symptom — agreeing with upstream
  is not evidence.
- Reporting the check you meant to run.
- Softening "the runbook is wrong" into "results were mixed".
