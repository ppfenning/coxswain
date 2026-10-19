---
description: Land an approved run, dry run first, then apply only when told to.
argument-hint: <run id>
---

Land an approved run.

Run `cox runs land $ARGUMENTS --repo <the run's repository>` as a dry run
(fall back to `agent-tools runs land` if `cox` is not on PATH), and show
the plan it prints. Stop there.

Only rerun the same command with `--apply` if the operator explicitly says to apply
it in this conversation; never decide to apply it on your own reading of
the plan.

Report the PR URL and the merge state once it lands. If the plan refuses,
say why it refused and stop there.

Never merge the run by another route.
