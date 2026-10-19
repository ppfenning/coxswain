---
description: Show the runs table, in flight, exited, and the latest events.
argument-hint: (no arguments)
---

Show the runs table.

Run `cox route leader status` (fall back to `agent-tools route leader
status`) first; if it errors or is not yet installed, say so and carry on
regardless. A LIVE result held by another session means the loop is owned
by `<holder>`; message it or resume it, do not re-arm on its runs — then
show the table anyway, since reading it takes no lock and arms no monitor.

Run `cox route status` (fall back to `agent-tools route status`) for what
is in flight and what has exited. Then run `cox runs events --runs-dir
<runs dir> | tail -20` for the latest events, using the runs directory
already established for this session from the docket or the profile; if
none has been established, ask rather than guess at one.

Close with one short paragraph on anything that needs a decision: a
quarantine, a budget stop, or a run stalled waiting on a human.
