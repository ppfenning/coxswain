---
description: Route a request through route-work; launch it if it changes a repository, else answer inline.
argument-hint: <what you want done>
---

Route this request through the route-work skill.

Decide first whether acting on it would change a repository. If not,
answer inline right here and stop.

If it would, size it against the cartridge's `epic_threshold`. Below it,
`cox route file` then `cox route launch epic`. At or above it, `cox route
file --intake`, then `cox route launch decompose`, then `cox route launch
epic` once the decomposed tasks have landed — never launch the epic over
an unscoped intake item. Fall back to the `agent-tools` form of each
command when `cox` is not on PATH.

Report the run id, the log path, and the command to watch it.

Never do the routed work yourself: once it is launched, it belongs to that
run, not to this session.

Request: $ARGUMENTS
