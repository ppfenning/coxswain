---
description: Move a queued intake item to ready, or hold an intake item or initiative.
argument-hint: <id> ready|hold [reason]
---

Move a queued intake item to ready, or hold an intake item or initiative.

Take the id and the target from the arguments. If either is missing, ask
rather than guess.

To ready it: only a queued intake item can be readied, and the chair refuses
the id of an initiative. If the id names an initiative, do not call either
route. A held initiative is freed with `cox route release <id>`, and a draft
initiative's todo tickets go through `/approve`; say which one applies and
stop. For an intake item, if `set_stage` appears in this session's tool list,
call it with the id and stage `ready`. If the tool is absent, or the call
fails because the server cannot be reached, run `cox route ready <id>` (fall
back to `agent-tools route ready` if `cox` is not on PATH).

To hold it: an intake item or an initiative can be held. If `send_directive`
appears in this session's tool list, call it with the id and kind `hold`. If
the tool is absent, or the call fails because the server cannot be reached,
run `cox route hold <id> --reason <reason>` (fall back to `agent-tools route
hold` if `cox` is not on PATH). Ask for the reason if none was given.

If the chair refuses the move, report the refusal and stop; do not retry it
through the other route. Say which route was used.

Arguments: $ARGUMENTS
