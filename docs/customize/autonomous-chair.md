# Autonomous chair

This page shows how to let the chair run its own routine work. It assumes
you have run Coxswain on one machine and know what the chair is.

## What it does

`cox chair run` is the chair's mechanical loop. Each tick, it does these
things in order. The default tick is 60 seconds.

- It beats the chair lease.
- It pulls the workspace checkout from its upstream (see
  [The workspace checkout](#the-workspace-checkout)).
- It reads the 5-hour window and the weekly spend.
- It lands a completed phase as one squash onto a fresh main, from
  `pr/<initiative>--<phase>`, one repository at a time. An approved task in
  a partial phase waits for the rest of the phase instead of landing alone.
- It relaunches an initiative once its ready tasks' dependencies have
  landed.
- It retries a quarantine caused by the harness once.
- It fills free lanes from the work store first, then decomposes intake in
  pairs.
- It fills lanes on the lane hosts too, after the local lanes. The lane
  hosts are the hosts table (`cox host add`), or the profile's `lane_hosts`
  while that table is empty.
- It runs housekeeping on a period: a lake sync, a trace prune, and a run
  clean.
- At most once a day it tunes each host's lane count in the hosts table,
  within that host's bounds, and sends any tier proposal to the inbox.
- It counts the drafts waiting for approval and never launches one.
- When the board lives in a shared store and the tick changed rows, it
  exports the board to `work/` and `intake/`, commits, and pushes the
  workspace.
- It prints one status line, and a `work state:` line naming its board when
  a shared store is configured.

The status line looks like this:

```text
chair 09-26 12:30 EDT | lanes 1/8 | lands 0 | limits 5h 20% weekly 75%/93% | needs chair: none
```

When the profile names a shared Postgres store, the loop also prints where
it reads the board from. If the store is unreachable, that line reads:

```text
work state: files (store unreachable)
```

A local SQLite store keeps the ticket files as the board and needs no shared
store; local-first stays the default.

## Running it

Start with a dry run. It plans one tick and changes nothing:

```sh
cox chair run --once --dry-run
```

To perform one tick, drop `--dry-run`:

```sh
cox chair run --once
```

To loop, run it with no `--once`. The `--interval` flag sets the seconds
between ticks:

```sh
cox chair run --interval 120
```

To keep it running, use a user service. This unit is an example of the
shape:

```ini
[Unit]
Description=Coxswain autonomous chair

[Service]
ExecStart=cox chair run
Restart=on-failure

[Install]
WantedBy=default.target
```

To write the real unit for your machine, run the install command. Every
flag is optional:

```sh
cox chair service --install [--label L] [--interval S] [--environment-file PATH] [--profile P]
```

It writes `~/.config/systemd/user/coxswain-chair.service`. The unit has
these lines:

- `ExecStart=<absolute cox> chair run --label L --interval S`
- `WorkingDirectory=<workspace_dir>`
- `Restart=on-failure`
- `RestartSec=30`

It adds `EnvironmentFile=-<path>` when you pass an environment file. It adds
the same line when `~/.config/agent-tools/garage.env` exists.

The command prints the unit path and three lines to run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now coxswain-chair.service
systemctl --user status coxswain-chair.service
```

The command runs no `systemctl` itself. You run those three lines.

To see the installed unit, run this:

```sh
cox chair service --status
```

It prints the unit text. If there is no unit, it prints `no unit at <path>`.

A user service stops when you log out. To keep the loop running while nobody
is logged in, run `loginctl enable-linger` yourself. Cox does not run it.

## cox console

`cox console` is one terminal screen of drafts, hosts, lanes, the chair and
needs-chair items, one line per item. Its keys run the matching `cox`
command after a y/n confirmation, so watching the fleet and acting on it no
longer needs a second terminal:

```sh
cox console
```

## Limits

The lane cap is `policy.dispatch.max_in_flight` in the team cartridge, and
it applies per machine, not across the fleet. A fleet gives each lane host
its own capacity with `cox host add <name> --ssh <dest> --capacity <n>`; the
Lane hosts section below shows how the loop fills each host in turn.

The weekly spend window is anchored at `spend.weekly_reset` in the profile's
`spend` block (for example `"Sun 04:00 America/New_York"`), not a rolling
seven days. The loop, the launch gate, `route context` and `usage assess`
all count the same week, and the status line shows where the week began.

Past the hard stop, the loop lands work but launches nothing. The hard stop
is `weekly_hard_stop_fraction` of `weekly_ceiling_usd`.
`weekly_ceiling_usd` lives in the profile's `spend` block.
`weekly_hard_stop_fraction` is read from `<runs>/policy.pacing.json`, a
JSON file you write in the workspace's runs directory; it is 0.93 when the
file or the key is absent. The same file may set `hard_stop_fraction` (the
5-hour window, default 0.99) and `max_in_flight`.

`role_ceiling_usd` is a separate failsafe on one build's own cumulative
spend, not a hard bound on any single action. The Claude Code provider
profile sets `role_ceiling_usd: {build: 6.0}`; the Build checkpoints section
below shows how a build spends against it.

The loop never cuts a release. It never merges a Homebrew tap PR. It never
edits a profile file and never applies a tier; it only proposes tiers. The
one setting it writes is a host's lane count in the hosts table. It pushes
only the workspace checkout, as described next.

To hold every land while you cut a release, freeze lands. A freeze lifts
itself when its minutes pass:

```sh
cox chair freeze-lands --for 60 --reason "cutting 2026.10.1"
cox chair freeze-lands            # show the freeze
cox chair freeze-lands --off      # lift it now
```

## The workspace checkout

The workspace (`work/`, `intake/`, `runs/`) is a git checkout. The loop
keeps it in step with its upstream on every live tick. A dry run does
neither step.

**Pull.** Right after the beat, the loop runs
`git pull --ff-only --autostash` in the workspace. Uncommitted changes are
carried across. If re-applying them conflicts, the loop puts the checkout
back where it was and restores the changes. A workspace that is not a git
checkout, or a branch with no upstream, is skipped. A failed pull never
stops the tick: the status line gains `workspace pull failed: <reason>`,
and the store records one `pull_workspace` action per distinct reason.

**Export, commit and push.** This step runs only when the board lives in a
shared store (`work state: store`) and the tick changed rows. The loop:

1. writes the changed rows from the store to their files under `work/` and
   `intake/`, leaving out files someone edited by hand (the export holds
   them);
2. stages `work/` and `intake/` (`git add -A`), without the held files;
3. commits the staged files as `chair: export <n> changed row(s)`;
4. pushes the current branch to its upstream
   (`git push <remote> HEAD:<branch>`).

Nothing else in the workspace is committed. No upstream, or no git
checkout, means no push. A failed `git add` or `git commit` shows as
`export failed: <reason>` on the status line; the rows stay changed in the
store. A failed push shows as `workspace push failed: <reason>` and never
fails the tick.

## Build checkpoints

A build runs in slices. Each slice stops at the guide, the build's measured
shape (about $1.90), where the harness judges go or no-go: the session can
resume, the partial work stays inside the task's surfaces, and the slice
changed something.

A build that passes its checkpoint resumes, as many times as it takes, until
the session's cumulative spend reaches the `role_ceiling_usd` failsafe ($6
on the Claude Code profile). Past it, the build is refused with a split
recommendation. The weekly cap described above stays the fleet's global
bound; `role_ceiling_usd` bounds one build's session, not the fleet.

## Housekeeping

Each tick, the loop runs housekeeping on a period and records it as a chair
action: a lake sync, a trace prune, and a run clean.

The lake sync is also where run logs are archived. `cox lake sync` archives
each ended run's text log and call log older than
`log_retention_days` (default 7) beside the traces on the object store,
reads each copy back, and only then removes the local file. The loop's
trace prune keeps the same number of days.

To keep logs longer, set `log_retention_days` in the routing profile. Every
machine needs it applied together, since an older `cox` refuses a key it
does not know.

## Lane hosts

Each tick, the loop counts the live lanes on every host. It reads them from
the leases in the store, in `runs.host`.

The lane hosts are the active rows of the hosts table. Add one with
`cox host add <name> --ssh <dest> --capacity <n> [--weight W]
[--capabilities a,b]`. While the table is empty, the profile's `lane_hosts`
list is used instead.

The loop fills the local lanes first, up to `policy.dispatch.max_in_flight`.
It then places the remaining epics on lane hosts in proportion to each
host's weight, up to each host's capacity, and only on a host whose
capabilities cover the initiative's tasks. Decomposes and pulls stay
local.

The loop launches a remote lane with `--on`:

```sh
cox route launch epic --initiative work/<id> --on <host>
```

The status line names a remote launch as `epic:<initiative>@<host>`:

```text
chair 09-26 12:30 EDT | lanes 8/8 | lands 0 | launched: epic:x@host-a | limits 5h 20% | needs chair: none
```

An approved task on a remote run gets a fetch before its land. The run has a
`<run>.remote.json` and no fetched marker. The loop runs `cox runs fetch
<run>` once per run, then lands.

The launch checks the login on the host first. If `claude` is not logged in
there, the launch is refused:

```text
routing: launch on <host> failed at auth: claude auth: not logged in on the host (run claude auth login there)
```

Log in on the host to clear it. [Several machines](several-machines.md)
shows how to set up a lane machine.

## Taking over

To take the chair yourself, run this from an interactive session:

```sh
cox route chair take --steal --hours H
```

The `--hours` flag defaults to `policy.leader.takeover_hours`, which is 3.
It stamps `until` on the chair record.

The loop sees the change on its next beat. It stops acting and reports who
holds the chair. Any action it planned under the lost lease is refused.

While you hold the chair, the loop's status line reads:

```text
standby: held by <session> until <time>
```

To move `until`, run this from the session that holds the chair:

```sh
cox route chair extend --hours H
```

A beat never moves `until`. Only `take` and `extend` do.

`cox route chair status` shows `until <time>` and the hours left. When the
time has passed, it shows `(expired)`.

Once `until` has passed, the loop's next tick takes the chair back. The
action is `take_lease`, with the reason `takeover expired at <time>`. A
forgotten takeover cannot park the fleet.

To hand the chair back:

```sh
cox route chair release
```

With a shared Postgres store, any machine on the network can take the
chair. [Several machines](several-machines.md) shows how to set that up.

## Rescue

A build can be quarantined for a harness reason and still keep its patch.
You can rescue it. Run this from `src/graphs` in the checkout:

```sh
python shell.py rescue --team <team> --initiative work/<id> --task <task-id>
```

The rescue re-applies the patch. It runs the configured checks itself, then
runs the review round. On approve, the task lands as usual.

A rescue runs at most once per version of the ticket.

## Drafts

A draft is an initiative under `work/<id>/`. Its `initiative.md` frontmatter
has `draft: true`, `proposed_by` and `proposed_at`. Its tasks are
`state: todo`.

Every part of Coxswain holds `todo`. The driver builds only `ready` tasks
whose needs are done. A draft therefore never builds until you approve it.

To write drafts, run the steward:

```sh
cox steward draft [--json]
```

It writes each grounded steward proposal in intake as a draft. It lists the
drafts that already exist. It lists the proposals it could not ground.

Not every draft starts with the steward. Stale work becomes a draft too, for
a person to approve or decline: a ticket with no recent commit is turned
into a draft naming `proposed_by` and `proposed_at`, the same fields a
steward draft carries. A ticket with no commit yet reads its file's
modification time instead, so a fresh ticket is never judged stale.

To approve a draft, run this:

```sh
cox route approve <initiative> [--task <id>]
```

It moves the draft's `todo` tasks to `ready`. With `--task`, it moves only
that task.

To decline a draft, give a reason:

```sh
cox route decline <initiative> --reason <text>
```

It moves the draft's tasks to `dropped`.

The loop never launches a draft. Two places show that drafts are waiting.

- The status line shows `drafts N` when N is above 0.
- `cox route status` ends with a `drafts:` list. Each row is
  `<id>  <proposed_by>  <age>`.

The tail of `cox route status` looks like this:

```text
drafts:
  my-initiative  steward  2h
```

## Watching the loop

`cox chair attach` says whether a live loop holds the lease. It exits 0 if
one does and 1 if not, and never touches the lease.

`cox chair watchdog` is a one-shot check for a five-minute timer. It reads
only the store and sends an alert when the loop has been idle or has not
beaten its lease for too long. `cox chair watchdog --install` writes the
timer files for this platform and prints the lines that activate them;
`--uninstall` removes them.

## What it records

Some actions the loop only records, such as a "needs the chair" report.
Each one is appended to `chair.actions.jsonl` in the runs directory. The
line carries its time and the lease epoch. When the profile names a store,
the same line is also written to the store's `chair_actions` table, so any
machine can read it.
