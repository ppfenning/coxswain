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

To keep it running, use a user service. This unit is an example only.
Adjust it to your machine:

```ini
[Unit]
Description=Coxswain autonomous chair

[Service]
ExecStart=cox chair run
Restart=on-failure

[Install]
WantedBy=default.target
```

Put the unit in your user unit directory.

## Limits

The lane cap is `policy.dispatch.max_in_flight` in the team cartridge.

Past the hard stop, the loop lands work but launches nothing. The hard stop
is `weekly_hard_stop_fraction` of `weekly_ceiling_usd`. Both live in the
spend settings of the routing profile.

The loop never cuts a release. It never merges a Homebrew tap PR. It never
pushes the workspace. It never changes a profile or a tier.

## Taking over

To take the chair yourself, run this from an interactive session:

```sh
cox route chair take --steal
```

The loop sees the change on its next beat. It stops acting and reports who
holds the chair. Any action it planned under the lost lease is refused.

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

## What it records

Some actions the loop only records, such as a "needs the chair" report.
Each one is appended to `chair.actions.jsonl` in the runs directory. The
line carries its time and the lease epoch.
