---
description: Land an approved run, dry run first, then apply only when told to.
argument-hint: <run id>
---

Land an approved run.

The command line is the route for landing: the chair's MCP server lists no
landing tool today, and a merge must stay behind the dry run and the
operator's word. Use an MCP tool for landing only if the server lists one in
this session's tool list, and keep the same dry run first and the same rule
about applying.

Run `cox runs land $ARGUMENTS --repo <the run's repository>` as a dry run
(fall back to `agent-tools runs land` if `cox` is not on PATH), and show
the plan it prints. Stop there.

Only rerun the same command with `--apply` if the operator explicitly says to apply
it in this conversation; never decide to apply it on your own reading of
the plan.

Report the PR URL and the merge state once it lands. If the plan refuses,
say why it refused and stop there.

Never merge the run by another route.
