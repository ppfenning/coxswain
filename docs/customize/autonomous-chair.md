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

## Intake queue control

The chair decomposes intake from the queue. You steer that queue with
priority, dependencies, a hold and a removal. None of them needs a hand edit
of an intake file.

### The order of decomposition

The chair decomposes queued intake by priority, highest first. Intake of equal
priority goes oldest first. A missing or non-integer priority counts as 0.

`cox route priority` orders a queued intake as well as an initiative. Name
the intake and pass `--set`, `--up`, `--down`, `--top` or `--bottom`:

```sh
cox route priority fix-login-redirect --top
```

### Waiting on other work with after

An intake can carry an `after` key. It lists the initiatives or intakes that
must land first. Set it when you file the intake:

```sh
cox route file --intake --repo coxswain --title "Add retry to sync" --body - \
  --after auth-rework,fix-login-redirect
```

Change it later with `cox route edit`:

```sh
cox route edit add-retry-to-sync --after auth-rework
```

Repeat `--after` or comma-separate the ids. On `route edit`, an empty value
clears the list. `--after` applies to a queued intake only. `route file`
refuses it without `--intake`.

The chair neither decomposes nor launches the item until every named
initiative or intake has landed. An initiative has landed when it has tasks
and every one is `done` or `dropped`. An intake has landed when its initiative
has landed. Once the last one lands, the chair decomposes the item on the
next tick. You do not edit anything.

An id that names nothing is unmet, so a typo holds the item back. A removed
intake never counts as landed. An item waiting on a removed intake stays
waiting until you clear or change its `after`.

### Reading the queue in route status

`cox route status` marks a waiting item in the queued intake group with the
ids it still waits on:

```text
add-retry-to-sync (waiting on auth-rework, fix-login-redirect)
```

Held items get their own `held:` heading after the groups. Each line is the
intake id and, when there is one, its hold reason. With `--json`, the output
gains a `waiting` map and a `held` list whenever either is non-empty.

### Hold and release

Hold is the reversible way out of the queue. The chair skips a held intake
until you release it. The reason is required:

```sh
cox route hold add-retry-to-sync --reason "waiting on the API freeze"
cox route release add-retry-to-sync
```

A held intake stays in `intake/` with `held: true` and a `hold_reason` line
that names who held it and when. The hold is written to the store row and to
the intake file, so it survives ticks and board exports. `release` removes
both keys. Only a queued intake can be held. Both commands take `--dry-run`,
which prints the plan and writes nothing.

### Removing an intake

Use remove to retire an intake for good. It moves the file to `intake/done/`
and records who removed it, when, and why. It takes several ids, or a
`--match` glob over each queued intake's file stem or id. Both forms need
`--reason`:

```sh
cox route remove old-spike another-spike --reason "superseded"
cox route remove --match 'spike-*' --reason "superseded" --dry-run
```

With `--dry-run`, remove prints what it would do and writes nothing. A glob
that matches nothing is refused. A refused target does not stop the rest, and
the command prints `removed N of M` and exits with the worst code. An
initiative with a live run is refused.

### Where the file-only read differs

The chair can also read intake from files alone, with no store rows. That read
cannot tell whether a dependency has landed, so it skips an item that has any
`after`. A held item is skipped too. The item runs through the store-backed
read, which can judge landing.

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

## Sharing files between initiatives

Initiatives may build together on a shared file. The limit is
`chair.max_shared_file_initiatives` in the profile. It defaults to 2. Setting
it to 1 restores the old behaviour, where the loop holds every initiative that
shares a file with one already building.

`serial_files` is a flat list of paths at the top level of the profile. The
loop always holds these files one initiative at a time, whatever the limit
says. A path ending in a slash covers the whole directory.

The loop also holds a file that both initiatives mark `(new)` in their planned
surfaces. That is the planned-scope estimate of a heavy rewrite. The loop does
not detect a `restructured` file beyond that marker.

```yaml
serial_files: [Cargo.lock, src/app.rs, migrations/]
chair:
  max_shared_file_initiatives: 2
```

Every land builds on current main, so the second land to touch a shared file
rebases by construction. When a cherry-pick conflicts, the task goes back to
`ready` with a `land_conflict` note. The build runs once more on the new main.

The land log records `conflict_paths` for each cherry-pick conflict. Count the
conflicts per path in that log. A path that conflicts often belongs in
`serial_files`, or under a lower limit.

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

### Starting a session beside the chair

A bare `cox` session decides whether to take the chair as it starts. The
`session_takes` key under `chair:` in the profile sets the policy. These are
the lines to add, at the top level of the profile file:

```yaml
chair:
  session_takes: when_free
```

The key accepts `never`, `when_free` and `always`, in any case. `when_free` is
the default and applies when the key is absent. Any other value makes the
profile unreadable.

With `never`, a session never takes the chair. It opens as a non-chair session
and leaves a free chair free. When another session holds the chair, it prints
who holds it and opens as a non-chair session without asking.

With `when_free`, a session takes the chair when it is free or its lease has
gone stale. When the last holder crashed and its Claude session can still be
found, the session resumes that conversation and takes the lease. When another
session holds the chair, the session shows an offer. The choices are to take
over, open a non-chair session or cancel. A fourth choice, attach, appears when
the holder's conversation is on this machine, and Enter picks it. Otherwise
Enter opens a non-chair session.

With `always`, a session takes or resumes a free or stale chair as `when_free`
does. It also takes the chair from another session without showing the offer.

None of the three values lets a session displace a live chair loop. The loop
may be the systemd service or another running chair loop. A session started
beside it opens as a non-chair session and prints one of these lines:

```text
chair held by <label> (service)
chair held by <label> (chair loop)
```

The first names the systemd service. The second names any other running chair
loop. The session takes, steals and releases nothing. This holds under
`always` too, because `always` only reaches sessions and never loops.

The one way to take the chair from a live chair loop is the explicit
`cox route chair take --steal` command shown above. Bare `cox` never runs it
for you. Only the operator's own command displaces a live loop.

A session that holds the chair releases its lease when it exits. The launcher
starts a background process that watches the session and checks it once every
five minutes. When the session is gone, that process ends the lease and clears
the chair record. A standby service takes the free chair on its next tick,
without waiting for the lease to expire. The release can lag the exit by up to
those five minutes.

Two beliefs are wrong here. A new session does not displace the service, even
under `always`. A held lease no longer waits out its expiry after the session
ends. The wait is now at most one five minute check.

## Lending the routine ticks

With nothing lent, a standby loop does nothing while a session holds the
lease. That is how it has always worked. You can change it for as long as you
hold the chair.

To lend the routine ticks to the loop, run this from the session that holds
the lease:

```sh
cox chair attach --lend-routine
```

To take them back:

```sh
cox chair attach --recall-routine
```

The two flags cannot be given together. Both act on the live holder only.

The lend is a mark on the lease record, the `routine_lent` key in
`chair.json`. Taking the chair never sets it. The mark dies with the holder.
When you release the chair or lose it, the next holder starts with nothing
lent.

While the mark is set, the loop still reports `standby`. It also plans the
routine ticks and nothing else. Launches, retries and rescues stay with you.

The loop relaunches an exited epic that has ready, unquarantined tasks. It
relaunches on the base the epic was launched with. It never relaunches while
a run for that initiative is alive.

The loop merges a fully approved level into the ticket branch once the repo
checks pass. It pushes only on a pass. When a check fails, it leaves the
level alone.

The planner allows these routine kinds and no others:

- `relaunch` starts an exited epic again, as above.
- `clear_branches` clears the old branches before a relaunch.
- `rebase_phase` rebases a phase just before its relaunch.
- `fetch` brings a remote run's branches home before anything builds on them.
- `fetch_exit` names a remote run that exited and has not been fetched.
- `land_phase` merges a fully approved level.
- `needs_chair` raises something for you to read.
- `waiting` reports an initiative blocked on other work.
- `deferred` reports a phase whose land is held until a later time.
- `merge_level`, `run_gate` and `steward_event` are the [steward's](#the-steward)
  merges, gates and events. They appear only when the steward is on.

Quarantines, budget stops and anything that needs a decision stay with you.
They show in the inbox.

## The steward

The steward runs an epic's routine steps without a model. It decides from
facts alone, and it reports each step as a typed event on the courier bus.

The steward is off by default. Turn it on with `chair.steward` in the
agent-tools profile. These are the exact lines to add. They go at the top level
of the profile file:

```yaml
chair:
  steward: true
```

The profile accepts `true`, `false`, `yes` and `no` in any case. `true` and
`yes` turn the steward on. `false`, `no` and an absent key leave it off, and the
loop gathers no steward facts. Any other value makes the profile unreadable, and
the error names the line.

The other chair keys sit in the same block and are numbers. They are
`housekeeping_hours`, `stale_days`, `idle_stall_minutes`, `launch_stagger_s` and
`max_launches_per_hour`.

The doctor report has a `steward` row. It shows `on` or `off`.

Do not turn `chair.steward` on yet. Today the profile setting is the only part
of the steward the run command wires. The loop does not yet pass the steward
these inputs:

- The bus entries it has already published. Without them the reported set is
  always empty, so a quarantine is announced again on every tick. The steward
  also relaunches on every tick and never reports `stuck`.
- The ticket branch head and whether a level has merged. Without them the head
  is blank and a settled level never counts as merged.
- The executor and the checks. Without them every merge and every gate is
  refused with `no steward executor`, and no `level_merged`, `merge_failed`,
  `gate_passed` or `gate_failed` event is published.

That wiring is separate work. The rules below describe what the steward
decides once those inputs are wired. This page changes when that work lands.

The steward follows these rules for each epic:

- It announces a quarantine once. A quarantined task raises `needs_attention`
  one time, and it is not raised again while the task stays quarantined.
- It merges an approved level once the build, vet and unit checks pass. The
  loop supplies those checks. The merge always comes before a relaunch.
- It relaunches an exited epic once for each state of its tasks. If the epic
  exits again with the same task states, it reports `stuck` and stops. It
  never relaunches while a task is quarantined.
- It runs the integration gate once for each ticket branch head. Gates run
  one at a time, so two epics never gate together. Under a hard stop the
  gate waits, because a gate run can spend what the stop protects. Merges and
  events still go ahead.

The steward acts when the loop holds the chair. It also acts while an
interactive session holds the chair, once that session has run
`cox chair attach --lend-routine`, because its merges, gates and events are
routine kinds. With nothing lent, a standby loop does nothing, and the steward
does nothing with it.

A dry run shows what the steward would do before it acts:

```sh
cox chair run --once --dry-run
```

Each steward action appears as a line such as `would merge level 0 of <epic>`
or `would run the gate of <epic>`. Nothing is merged, relaunched or published.
The dry run shows the plan. It does not show whether the executor is wired.

### The merge step by land_at

The steward's merge step follows the initiative's `land_at` setting. The three
values merge a level in three different ways.

At `ticket`, the steward merges the level into `pr/<initiative>`. The steward
creates that branch from the target branch the first time the initiative needs
it.

At `level`, the level lands as its own pull request, as a phase land does. The
steward first fetches the runs that hold the level's approved tasks. A phase
that was split lands one task at a time, each from that task's newest run. The
land honours the land lock and waits for green checks.

At `task`, the steward lands each approved task.

One initiative's merge failure publishes `merge_failed` for that initiative. It
never stops the steward from handling the other initiatives. The initiative's
normal relaunch still runs after the failure.

`run_gate` still reads `pr/<initiative>`. An initiative that lands at `level` or
at `task` has no gate branch yet, so the gate has nothing to read for it.

### The events

The steward publishes eight typed events. Each is published under the epic's
id as its topic.

| Event | Fields | Wakes a model |
| --- | --- | --- |
| `needs_attention` | `task` | yes |
| `level_merged` | `head`, `level` | no |
| `merge_failed` | `head`, `level` | yes |
| `relaunched` | `signature` | no |
| `stuck` | `signature` | yes |
| `gate_passed` | `head` | no |
| `gate_failed` | `head` | yes |
| `epic_complete` | `head` | no |

`head` is the ticket branch head. `level` is the index of the level. `signature`
is the sorted task states at the time. `task` is the id of the quarantined task.

The fields of `merge_failed` and `gate_failed` are not fixed by the steward.
The executor chooses them, and it falls back to the fields of the merge or
gate action, which are the fields shown above. Read them by name and ignore
any you do not know.

A model is called only at epic start, at close and for the four events marked
yes. The other four are read and acted on without one.

A consumer reads the events by topic and event, and it acks under its own
consumer name. This lists the entries that need a model:

```sh
cox courier inbox --topic <epic> --event stuck --consumer <name> --wake
```

This acks one entry for that consumer only:

```sh
cox courier ack <id> --consumer <name>
```

An ack under one consumer name hides the entry from that consumer and from no
one else.

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
