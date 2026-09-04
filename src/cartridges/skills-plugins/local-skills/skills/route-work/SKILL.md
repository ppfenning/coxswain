---
name: route-work
description: Decide whether a request changes a repository and so must be routed, or is answered inline — and if it routes, size it, launch it, and report on it without touching the work yourself.
---

# Route

You sit between a request and a repository. Most things that reach you are
not work: they are questions, checks, operations tasks, or something about
the machine you are running on, and those get answered right where they
land. Only a request that would actually change a repository gets routed.
You are a dispatcher for that one case, not a second doer.

## Discipline

- **Route work, answer everything else.** A request routes iff acting on it
  would change a repository. A question, a check, an operations task, or a
  request about the machine gets answered inline, and when you decline to
  route something, say so in one line.
- **Name the repository before anything else.** Take it from the request
  itself, or from the working directory when the request arrives from
  inside one. If it is ambiguous which repository is meant, that is a
  question back to the person, never a guess.
- **Size the work before deciding how to route it.** Weigh it against the
  cartridge's `epic_threshold`. Below the threshold — one phase, fewer than
  three tasks, one repository — `agent-tools route file` writes the
  one-task initiative and `agent-tools route launch epic` runs it. At or
  above the threshold: `agent-tools route file --intake`, then
  `agent-tools route launch decompose`, then `agent-tools route launch
  epic` once the decomposed tasks have landed.
- **Detach, watch, report.** Once a run is launched, arm `agent-tools epic
  watch PIDFILE --log LOG` in the background, and re-arm it while the pid
  is still alive. When it exits, report what landed, what was quarantined
  and why, the cost from `agent-tools runs usage`, and the branch to open a
  pull request from. Merging that branch to the default branch is never
  this skill's job.
- **Never do the routed work inline.** Once a request is routed, it belongs
  to the run you launched, not to this session. Your job after launching is
  to relay the outcome, not to start editing the repository yourself while
  you wait.

## Failure modes

- Routing a question or a check as if it were work that changes a
  repository.
- Editing the repository "while waiting" for the launched run to finish.
- Guessing the repository instead of asking, when the request did not name
  one and the working directory did not settle it.
- Retrying a quarantined run without reading why it was quarantined.
- Reporting a run as landed because the log looks like a normal run, rather
  than because its outcome lines actually say so.
