---
description: Approve a draft initiative's todo tickets.
argument-hint: <initiative id>
---

Approve a draft initiative's todo tickets, so todo becomes ready.

Take the initiative id from the arguments. If none is given, ask rather than
guess.

The chair's write tools have no approve tool, so the command line is the
route: run `cox route approve <initiative id>` (fall back to `agent-tools
route approve` if `cox` is not on PATH). Switch to an MCP tool only if the
server lists one for approval in this session's tool list; if it does, call
it, and if that call fails because the server cannot be reached, run the
command line instead.

If the chair refuses the approval, report the refusal and stop; do not retry
it through the other route. Say which route was used.

Arguments: $ARGUMENTS
