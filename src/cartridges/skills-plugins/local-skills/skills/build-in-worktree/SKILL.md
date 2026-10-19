---
name: build-in-worktree
description: Execute a plan and return the change as a unified diff. The worktree is yours; nothing else is.
---

# Build

You do the work for one planned item and return it as a **unified diff**. You
never apply the change anywhere: the shell owns a disposable worktree and
applies your patch there, on the far side of a human gate. Being wrong here
costs nothing — that is by design, so use the freedom to be precise rather
than cautious.

## Discipline

- **Follow the plan.** The plan's `files_expected` is your boundary. If doing
  the work correctly requires touching a file the plan never named, say so in
  `summary` — do not smuggle it in silently. The reviewer diffs your
  `files_touched` against the plan.
- **The patch is the deliverable.** Well-formed unified diff, minimal context
  drift, no unrelated hunks. A patch that mixes the fix with reformatting
  buries the fix.
- **`commands_run` is evidence, not decoration.** Record what you ran and what
  it actually printed. If you could not run something, record that as its
  output — an honest "not run: no test runner in context" beats a remembered
  pass. Never report output you did not see.
- **Match the codebase you were shown.** Its naming, its idioms. A technically
  correct patch in a foreign style is a revise. Comment density is the one
  thing not to match: write lean even beside verbose neighbours.
- **Write lean.** The typed signature is the documentation. A docstring is one
  line, and only where the signature cannot say it — a rule, a unit, an
  invariant, an ordering. No comment narrates what the next lines do. A pure
  function is deterministic, so one literal call per rule is its test; a test
  per branch, or one that restates the implementation, is weight the reviewer
  strikes. Spend the lines on the function, not on prose about it.

## Test data

In any repository that ships a `.github/check_clean.py`, tests and fixtures use
only neutral stand-ins. The one neutral home directory is `/home/user`, the
home folder of a user named `user`. Hosts are example names with no `.lan`
suffix. Ssh users and operator or team settings are a neutral word such as
`dev` or `acme`. No private-network or tailnet address appears.

CI's clean check rejects anything else, and the names rule reads a pattern you
cannot see, so the safe move is never to use a real name or path.

## What good looks like

The smallest patch that satisfies the plan, with a summary that says what
changed and why in two sentences, and command output a reviewer can trust.

## Failure modes

- A patch that "also" fixes things the plan put out of scope.
- Claimed test results with no command recorded.
- A diff that will not apply because it was written from memory of the file
  rather than the file as given.
- Test data treated as harmless because only a test reads it: a real home
  path, a real person's name as an ssh user or sample setting, a `.lan` host.
  The review is green, then CI's clean check fails the test job, and the task
  costs a quarantine and a hand fix.
