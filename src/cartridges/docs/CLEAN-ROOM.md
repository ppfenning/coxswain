# Clean-room discipline

This repository is written from knowledge, not from files.

## Why this file exists

The layered-cartridge pattern was worked out while I was employed elsewhere. Two
different things came out of that period, and they have different owners:

| Thing | Who owns it |
|---|---|
| The **specific files** I wrote in the employer's repos — the actual text of a SKILL.md, their runbook taxonomy, their board wiring | The employer |
| The **knowledge** — what I believe about functional Python, how I think epics should decompose, why an agent should propose rather than write | Me, permanently |

The second category is what lawyers call general skill and knowledge, and it is
not something an employment agreement takes away. It travels with the person.
The first category does not travel, and I do not want it here.

## The rule

**Write from your head, not from a source file.**

If you are looking at a prior file while writing, you are copying its expression.
If you are writing what you believe, from memory, in your own words, you are
exercising your own knowledge. The output is usually better anyway, because you
rewrite rather than transcribe, and the second pass at an idea is sharper than
the first.

Practically, for this repo:

- Nothing here is copy-pasted from a prior employer's repository.
- Test fixtures are synthetic. Never sample a real workspace, tracker, or
  customer — fixtures are the single most-forgotten leak vector.
- No customer names, no tracker IDs, no bucket names, no colleague names, no
  incident narratives. Not even as examples. Use obvious fakes (`acme`,
  `example-team`, `1234567890`).
- If a concept can only be explained by reference to a specific former
  employer's system, it does not belong in the base layer. Generalize it or
  drop it.

## What that leaves

More than it sounds like. Architecture is knowledge. The idea that a cartridge
should deep-merge base-under-team, that context packs concatenate base-first,
that write kinds carry a risk and a ramp, that autonomy is earned per-kind
against a ledger and reset by config change — all of that is design conviction,
and re-implementing it from conviction is both legitimate and faster than
scrubbing would have been.
