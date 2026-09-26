# Autonomous chair

This page shows how to let the chair run its own routine work. It assumes
you have run Coxswain on one machine and know what the chair is.

## What it does

`cox chair run` is the chair's mechanical loop. Each tick, it does these
things in order. The default tick is 60 seconds.

- It beats the chair lease.
- It reads the 5-hour window and the weekly spend.
- It lands every approved task, one repository at a time.
- It relaunches an initiative once its ready tasks' dependencies have
  landed.
- It retries a quarantine caused by the harness once.
- It fills free lanes from the work store first, then decomposes intake in
  pairs.
- It fills lanes on the hosts named in `lane_hosts` too, after the local
  lanes.
- It counts the drafts waiting for approval and never launches one.
- It prints one status line.

The status line looks like this:

```text
chair 09-26 12:30 EDT | lanes 1/8 | lands 0 | limits 5h 20% weekly 75%/93% | needs chair: none
```

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

## Limits

The lane cap is `policy.dispatch.max_in_flight` in the team cartridge.

Past the hard stop, the loop lands work but launches nothing. The hard stop
is `weekly_hard_stop_fraction` of `weekly_ceiling_usd`. Both live in the
spend settings of the routing profile.

The loop never cuts a release. It never merges a Homebrew tap PR. It never
pushes the workspace. It never changes a profile or a tier.

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
