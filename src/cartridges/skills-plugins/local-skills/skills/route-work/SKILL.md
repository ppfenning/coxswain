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

- **Open by checking the chair.** Before anything else, run `cox route
  chair status` (fall back to `agent-tools route chair status`). A LIVE
  result naming another session's label means the loop is owned by
  `<holder>`; message it or resume it, do not re-arm — and stop there: take
  no lock, arm no monitor, launch nothing. A LIVE result naming your own
  label, a STALE result, or NONE clears you to route. If the command errors
  or is not yet installed, say the check is unavailable and stop there too
  — do not treat a failed or missing check as a clear lock. Take the lock
  with `cox route chair take` only once cleared. The chair subcommands are
  `status`, `take`, `beat`, `release`, `clear` and `chat`.
- **Route work, answer everything else.** A request routes iff acting on it
  would change a repository. A question, a check, an operations task, or a
  request about the machine gets answered inline, and when you decline to
  route something, say so in one line.
- **Name the repository before sizing or routing it.** Take it from the
  request itself, or from the working directory when the request arrives
  from inside one. If it is ambiguous which repository is meant, that is a
  question back to the person, never a guess.
- **Size the work before deciding how to route it.** Weigh it against the
  cartridge's `epic_threshold`. Sizing has three outcomes: inline, a
  one-task initiative, or an intake plus decompose. Below the threshold —
  one phase, fewer than three tasks, one repository — `agent-tools route
  file` writes the one-task initiative and `agent-tools route launch epic`
  runs it.
  - **Inline.** A change is inline when all four hold: its repository is
    listed in `epic_threshold.inline.repos`; it touches at most
    `epic_threshold.inline.files_max` files; the fix is fully specified;
    and its tests run locally. With the default empty
    `epic_threshold.inline.repos` list, nothing is inline. An inline change
    is not filed or launched. You work it like any regular session: a
    branch, the repository's own checks, a pull request, a merge on green
    under the chair's merge authority, and a one-line note in the session.
  - **Anything else.** A change that does not meet all four conditions
    routes exactly as before. At or above the threshold:
    `agent-tools route file --intake`, then `agent-tools route launch
    decompose`, then `agent-tools route launch epic` once the decomposed
    tasks have landed.
- **Launch with the path and the label.** `route launch epic --initiative`
  takes the `work/<id>` path, never the bare initiative id. A bare id fails
  with `routing: no initiative.md at <id>/initiative.md`. When the chair is
  held by your own session, `route launch epic` refuses unless you pass
  `--label <chair label>`, the label that `chair status` shows.
  - **Let the launch place the lane.** Launch without `--on`. The launch
    places the lane by the hosts table and prints `placed on <host> (<in
    use>/<cap>)`. Trust that line. Do not default to your own machine. When
    no host has room, the launch refuses rather than overfill.
  - **Pin only on purpose.** `--on <host>` still pins a host. `--on local`
    with `--force` is the only way to exceed this machine's capacity.
- **Detach, watch, report.** Once a run is launched, arm `agent-tools epic
  watch PIDFILE --log LOG` in the background, and re-arm it while the pid
  is still alive and the chair lock still names you. When it exits, report
  what landed, what was quarantined and why, the cost from `agent-tools
  runs usage`, and the branch to open a pull request from. Merging that
  branch to the default branch is never this skill's job.
- **Keep your own context clear by backgrounding most in-session work.**
  The operator's intent: the chair should background most tasks so its
  context window stays clear, by whatever means fits, named or sub agents
  within the session if not passing to a coxswain. The exit watcher above
  is one case of this rule. This rule covers what you do in-session
  instead of routing. Work that changes a repository still routes to the
  lanes as before.
  - **Background it** when it is an investigation, a multi-step
    diagnostic, log or CI digging, a land and the waits around it, or
    anything that reads more than a few files. Use a background subagent
    or a background shell job: a named agent, a fork, or
    `run_in_background`. Keep only the conclusion in your context. Relay
    it to the operator in a few lines, not the agent's full report.
  - **Examples to background.** A CI failure dig. A land plus its wait. A
    sweep across run logs. Filing an intake that needs file reading.
  - **Do it yourself** when it is a short single lookup or decision: one
    status command, one file you already know, a yes or no on a land, or
    choosing what to launch.
  - **Examples not to background.** One `cox route chair status`. One
    priority set. A merge decision on a green PR.
- **Never do the routed work inline.** The one exception is the `inline`
  size above, which is worked in this session and never routed. Once a
  request is routed, it belongs
  to the run you launched, not to this session. Your job after launching is
  to relay the outcome, not to start editing the repository yourself while
  you wait.
- **Read the intake link before offering an idea as new work.** An intake
  file whose frontmatter already carries an `initiative` field is decomposed,
  not queued: its work is already routed, and there is nothing left here to
  route for it. Only a file with no `initiative` field is candidate work.

## Failure modes

- Routing a question or a check as if it were work that changes a
  repository.
- Editing the repository "while waiting" for the launched run to finish.
- Guessing the repository instead of asking, when the request did not name
  one and the working directory did not settle it.
- Retrying a quarantined run without reading why it was quarantined.
- Reporting a run as landed because the log looks like a normal run, rather
  than because its outcome lines actually say so.
- Re-arming an exit monitor, or launching anything, once `cox route chair
  status` reports the lock live under a name that is not yours.
- Treating a `cox route chair status` that errors or is missing as
  evidence the lock is clear, rather than stopping and saying so.
- Passing a bare initiative id to `--initiative`, or omitting `--label` when
  the chair is held by your own session.
