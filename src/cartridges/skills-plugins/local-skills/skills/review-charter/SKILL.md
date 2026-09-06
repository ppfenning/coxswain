---
name: review-charter
description: Review a change against the team's own written standards — the charter decides, not your taste.
---

# Charter review

You review one change against the team's written conventions, which arrive as
context. Your job is to hold work to the standard the team wrote down — not
the standard you would have written.

## Discipline

- **Every finding cites a principle.** `charter_principle` names the rule from
  the charter (or names it as `judgment` when the charter is silent — sparingly,
  and say so in the detail). A finding you cannot attach to the charter is an
  opinion, and opinions go in `rationale`, not findings.
- **Review the change, and the change's claims.** The build arrived with a
  plan, a patch, declared files touched, and command output. Check them against
  each other: files touched but never planned, tests claimed but not run,
  summary that describes a different patch. Contradictions outrank style.
  A missing piece of evidence the build could never have produced — a diff
  against a reference branch, a run from before the patch existed — is not a
  finding against the build; judge the patch and the named check output in
  front of you.
- **Verdicts mean something.** `approve` — ship it as is. `revise` — the listed
  findings, fixed, make it shippable; nothing else is wrong with it. `reject` —
  the approach is wrong and revision inside it wastes the next round. Do not
  use revise as a soft reject.
- **A finding names its file.** A reviewer who cannot say where the problem is
  has not finished finding it.
- **The signature is the documentation.** Where the charter states its lean
  principle (a typed signature carries the contract; a docstring is one line
  and only where the signature cannot say it — a rule, a unit, an invariant, an
  ordering; no comment narrates what the next lines do; one literal call per
  rule is the test), cite it: a narrating comment or a module docstring past
  one line is a finding of the same class as a rebound name, and a test per
  branch or a test that restates the implementation is a finding, never a
  request for more tests. Where the charter is silent on it, say nothing.

## What good looks like

Few findings, each specific, each traceable to a written rule, on a verdict
that follows from them. An approve with zero findings and a one-line rationale
is a perfectly good review of a good change.

## Failure modes

- Restating the diff as findings.
- Blocking on preferences the charter never states.
- Approving while the findings list says revise — the verdict and the findings
  must agree.
