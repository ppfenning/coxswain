# Autonomous chair

This page shows how to let the chair run its own routine work. It assumes
you have run Coxswain on one machine and know what the chair is.

## What it does

`cox chair run` is the chair's mechanical loop. Each tick, it does these
things in order. The default tick is 60 seconds.

- It beats the chair lease.
- It reads the 5-hour window and the weekly spend.
- It lands a completed phase as one squash onto a fresh main, from
  `pr/<initiative>--<phase>`, one repository at a time. An approved task in
  a partial phase waits for the rest of the phase instead of landing alone.
- It relaunches an initiative once its ready tasks' dependencies have
  landed.
- It retries a quarantine caused by the harness once.
- It fills free lanes from the work store first, then decomposes intake in
  pairs.
- It fills lanes on the hosts named in `lane_hosts` too, after the local
  lanes.
- It runs housekeeping on a period: a lake sync, a trace prune, and a run
  clean.
- It counts the drafts waiting for approval and never launches one.
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
is `weekly_hard_stop_fraction` of `weekly_ceiling_usd`. Both live in the
spend settings of the routing profile.

`role_ceiling_usd` is a separate failsafe on one build's own cumulative
spend, not a hard bound on any single action. The Claude Code provider
profile sets `role_ceiling_usd: {build: 6.0}`; the Build checkpoints section
below shows how a build spends against it.

The loop never cuts a release. It never merges a Homebrew tap PR. It never
pushes the workspace. It never changes a profile or a tier.

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

The loop fills the local lanes first, up to `policy.dispatch.max_in_flight`.
It then fills each `lane_hosts` entry in profile order, up to the same cap.
Decomposes and pulls stay local.

The loop launches a remote lane with `--on`:

```sh
cox route launch epic --initiative work/<id> --on <host>
```

The status line names a remote launch as `epic:<initiative>@<host>`:

```text
chair 09-26 12:30 EDT | lanes 8/8 | lands 0 | launched: epic:x@jarvis | limits 5h 20% | needs chair: none
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
You can rescue it. Run this in the harness:

```sh
python shell.py rescue --initiative work/<id> --task <task-id>
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

## What it records

Some actions the loop only records, such as a "needs the chair" report.
Each one is appended to `chair.actions.jsonl` in the runs directory. The
line carries its time and the lease epoch.
