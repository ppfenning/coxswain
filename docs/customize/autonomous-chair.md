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
- It sends every other quarantine to triage before the task is built again,
  and holds a task for you once it has failed too often. See
  [Quarantines](#quarantines).
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

The loop also prints where it reads the board from, which is always the
store. If the store is unreachable, that line reads:

```text
work state: store (unreachable)
```

Nothing stands in for the store. Commands that need it fail and say so
until it answers, and no state is written to the ticket files in the
meantime. The store is Postgres; a local SQLite store and the `files` work
state are retired.

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

The workspace (`work/`, `intake/`, `runs/`) is a git checkout by default. The
loop keeps it in step with its upstream on every live tick. A dry run does
neither step.

With a shared store the checkout is not needed. The store holds every intake,
initiative and task, text and state, and `work/` and `intake/` are files
written from it. Turn the git steps off in the profile:

```yaml
chair:
  workspace_git: false
```

The loop then pulls nothing and commits and pushes nothing. It still writes
the files, so everything that reads them keeps working, and they are this
machine's own copy. Any machine refreshes its copy from the store with:

```sh
cox route export
```

With git off, the tick reads its work from the store's rows, rendered as the
files would be, and opens no work file. A decompose writes its plan to the
store as rows on whichever machine ran it, a lane host included: a detached
launch runs the same command in the background, to completion. The export
still loads any ticket file the store has not got before it writes, so a
ticket written by hand as a file is not deleted.

You rarely need to run that by hand. With git off, `cox route`, `runs`,
`lander`, `epic`, `console` and `dash` check the store's version first and
rewrite the files only when a row changed since the last write. A file that
is an unchanged copy of a row the store has since dropped is deleted, so a
machine that was behind never puts the row back. A store that cannot be
reached leaves the files as they are and the command still runs.

**Cartridges stay yours.** They are written and versioned where you keep
them; the store never becomes the place they are authored. With git off and
a shared store, a launch to a lane host publishes the exact files of the
chair's `cartridges/` directory to the store under the digest of those
files, and the lane host writes that snapshot itself before the run. Nothing
is copied from machine to machine, and a host that cannot produce the same
digest stops the launch. A publish that fails falls back to the copy.

```sh
cox route cartridges publish             # prints the sha
cox route cartridges fetch --sha <sha>   # writes it into cartridges_dir
```

A fetch refuses to write over cartridges a person put there or edited. Only
a directory that is empty, or still exactly what the last fetch left, is
replaced without `--replace`.

With a shared store the courier bus is the store's `courier_log` table, one
bus for every machine, with git on or off. The first write loads what
`courier.jsonl` already held. A read never writes: until that first write, it
shows the file's lines, and it never migrates the store, so `cox chair run
--dry-run` changes nothing. `cox courier send`, `inbox` and `ack` work as
before.

The rest of this section describes the default, with git on.

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

### Queue workers

Four kinds of the tick's work can run as their own process instead:

- housekeeping: stale tickets, lane tuning, stalled runs, idle lanes, lapsed
  logins, and the periodic sweep above;
- the watch: reverting a landed commit whose main CI went red;
- quarantine: the triage of a failed task (a triage request, a hold for a
  person, or a `needs chair` line), restored approvals, widened tickets, held
  files, repeated failures and review PRs;
- decompose: launching a decompose for a new intake, and the reports of empty
  and stalled decomposes.

Launches, relaunches, retries and rescues stay on the tick. The tick stays the
default.

```yaml
chair:
  queues: [housekeeping, watch, quarantine, decompose]
```

With a kind listed, the tick no longer plans it, and
`cox chair worker --kind <kind>` does. A worker holds
the store lease `chair:<kind>`, timed by the store's clock, so a second copy of
one kind stands by, tries again every 10 seconds, and takes over within a
minute of the first one stopping. A worker that has just taken the lease waits
`--settle` seconds (default 30) before its first pass.

The tick leaves a kind alone while a worker holds its lease, whatever the
tick's own profile says, and checks the leases again just before it performs.
When it cannot read the leases at all, and a worker could be running, it
leaves every queue kind alone for that tick rather than risk doing one twice. A
worker could be running when the profile lists a kind, or when a `chair:<kind>`
lease row has ever been seen on this machine. The tick records that in
`<runs_dir>/chair-queue-lease-seen`, so a restarted tick still knows it. When a listed kind has had no live worker
for over a minute, the tick raises one `needs chair` line, `queue_unowned
<kind>`, and the notifier pushes it once.

A worker wakes on a write to a table its kind reads: `work_items` and `runs` for
housekeeping; `landing_records` and `land_queue` for the watch; and
`work_items`, `runs` and `phase_queue` for quarantine and decompose. A phase
that finishes moves its `phase_queue` row, which wakes the triage within
seconds, so no `run_exited` courier line is needed for it. It passes at
most once per `--min-gap` seconds (default 30), and at least every `--poll`
seconds (default 60) when nothing is written. Host and worker beats do not wake
it. While the profile does not list its kind, a worker stays idle.
`cox chair worker --kind K --once --dry-run` plans one pass and performs
nothing, so its plan can be compared with `cox chair run --once --dry-run`.

`cox setup doctor` has a `store notify queue` row on a Postgres store. It
fails when more than half of the server's notification queue is in use: a
session listening without draining fills it, and a full queue fails every
write to a watched table.

Every machine needs a `cox` that knows `chair: queues:` before any profile
sets it. An older `cox` refuses a profile with the key, so a rollback to one
also takes the key out. Only the flow list form, `queues: [a, b]`, is
accepted.

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

A bare `cox` launch runs in one of two modes, and it picks the mode before it
reads anything else about the chair.

With a live chair service holding a fresh lease under another label, the
launch attaches. It opens a Claude Code session with the chair's MCP server
configured and resumes the operator's stored conversation with the chair. It
works from any directory. It never reads the chair record to take it, never
takes or steals the lease, and starts no beater. The [MCP server](mcp.md)
page lists what the attached session can read and change.

With no live chair, the launch starts the workspace session that can act as
the chair, so a one-machine setup works as it always did. The rest of this
section describes that session.

The reason for the split: since the chair became the `coxswain-chair`
service, a session started in the workspace only converses, and a workspace
session that resumed could take the service's lease. Attaching over MCP
removes that risk, and lets any Claude client that has the server configured
do the same.

The `--workspace` flag forces the workspace session even when a chair is
live. The `--print-argv` flag shows which mode would run, and its argv,
without starting anything. `cox session` takes both flags too.

The coxswain plugin's operator commands (intake, stage, approve, land and
status) call the MCP tools when the server is reachable and use the command
line otherwise. So a plain Claude Code session with the server configured
behaves like the attached one.

The attached session stores its conversation id in `mcp-conversation.json`
under `coxswain` in the state directory (`$XDG_STATE_HOME`, else
`~/.local/state`). Every entry it sends to the chair carries that id.

A workspace session decides whether to take the chair as it starts. The
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
may be the systemd service or another running chair loop. A bare launch
beside it attaches over MCP, as described above. A workspace session forced
with `--workspace`, or started beside a loop that carries the session's own
label, opens as a non-chair session and prints one of these lines:

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

Quarantines, budget stops and anything that needs a decision stay with you.
They show in the inbox.

## The lander

The chair's tick does not land initiatives. A second worker, the lander, runs
beside it and owns the land queue:

```sh
cox lander run --interval 30
cox lander service --install
cox lander status
```

When a phase is built, the lander merges it into the initiative branch. When
every phase has merged, it takes the initiative to its parent as one pull
request. A conflict or a red check goes back to build as a rectify task. The
[landing page](../methodology/landing.md#landing-granularity) describes the
model.

The lander tells the chair with a `needs_attention` entry on the courier when
a phase is blocked, when a conflict cannot be sent back to build, and when a
queue item has waited for an hour. A consumer reads typed entries by topic and
event, and acks under its own consumer name:

```sh
cox courier inbox --topic <initiative> --event needs_attention --consumer <name> --wake
cox courier ack <id> --consumer <name>
```

An ack under one consumer name hides the entry from that consumer and from no
one else.

The epic steward that used to merge levels and run a gate is removed. A
profile that still sets `chair.steward` is read and ignored, and the doctor
report's `steward` row says it is retired.

## Quarantines

A run quarantines a task it could not finish. Each quarantine carries a
cause, and the chair handles it by that cause without asking you.

| Cause | What the chair does |
|---|---|
| `harness` | Retries the task, or rescues its kept patch, once for each run that quarantined it. |
| `code`, `review`, `ticket`, `unknown` | Asks for triage. The task is not built again until a model has looked at it. |

A `harness` cause is a stoppage, not a fault in the task. It covers a usage
limit, an expired login, an infrastructure failure, a stale base, an
interrupted run, and an empty build of a task whose base was stale.

The other causes mean the task needs a change before it is built again.

### Where the cause comes from

With `work_state: store`, the board lives in the store and no ticket file
holds an attempt. The chair reads each attempt from the run's task record
instead. A run writes one record for each task, at
`runs/<run>/tasks/<phase>/<task>.json`, and puts the cause of a quarantine
on it. The newest record names the run and the reason. A record with no
cause is not an attempt.

A run writes the ticket's body hash on the record too. An attempt on an
earlier version of the ticket then does not count against the amended
one. A record without the hash still counts toward triage and the hold,
but not toward the stale check.

A run on a lane host keeps its records on that host until the chair
fetches the run. The fetch copies the whole run directory, records
included, and the next tick reads them. Until then, a quarantine from an
earlier run of that initiative waits, so the chair never judges a task by
an older record. The status line shows each one, as
`awaiting fetch: <initiative>:<task> (run <run>)`. A quarantine from a
later run does not wait.

A quarantine with no record at all shows the cause `quarantined` and goes
to `needs chair`.

### Triage

To ask for triage, the chair sets `triage_requested` on the task's row in
the store and moves the task back to ready. It does this once for each run
that quarantined the task. The initiative then relaunches as usual. That run
triages the flagged task before it builds anything, whatever the task's
attempt count, and clears the flag. A run that cannot read the store rows
cannot see the flag, so it builds no task that has failed before and
leaves it for the next run.

The triage model reads the task's attempt record and the whole initiative:
its goal, each phase and its goal, and every other task with its state,
title and needs. It must cite the record. It answers with one class:

| Class | What happens |
|---|---|
| `ticket_defect` | An instruction that quotes the objection is appended to the ticket, and the task is built again in the same run. |
| `drop` | The task is dropped, because the initiative's goal does not need it. Any task that needed it stops needing it. |
| `combine` | The task's scope is appended to a named sibling task in the same phase, and the task is dropped. The tasks that needed it now need the sibling. |
| `shape` | The task is held for you with the split the model proposes. |
| `platform_defect` | An intake is filed for the harness bug, and the task is held for you. |
| `hold` | The task is held for you with the model's diagnosis. |

If the model gives the same class and diagnosis that an earlier triage of
the task gave, the task is held for you.

### The hold limit

After a task has failed a set number of times, the chair holds it for you
and asks for no more triage. The limit is `chair.quarantine_hold_after` in
the profile. It defaults to 3. Setting it to 0 turns the limit off.

```yaml
chair:
  quarantine_hold_after: 3
```

The count covers every version of the task's ticket, so an amended ticket
keeps its earlier failures. A stoppage and a failed rescue do not count.

The chair also holds a task that comes back quarantined in the same run it
already asked triage for. A triage that could not run no longer quarantines
the task: the epic holds it itself, and the hold reason starts with
`triage could not run:` and names what failed.

### The no-progress hold

The chair holds an initiative for you when its last runs all ended the same
way. Two runs end the same way when every phase has the same state (such as
`partial` or `blocked`) and the runs recorded no new task state. Another run
would end the same way, so the chair stops relaunching it.

The number of runs is `chair.no_progress_after` in the profile. It defaults
to 2. Setting it to 0 turns the hold off.

```yaml
chair:
  no_progress_after: 2
```

The held initiative's reason starts with `needs you: no progress:`. It names
the runs and the outcome they repeated, with the reason of the first phase
that did not complete. The inbox gets one `needs_attention` entry on the
courier for the hold.

Fix the cause, then release the initiative:

```sh
cox route release <initiative>
```

The release marks the initiative's newest run. Runs up to that mark no longer
count, so the chair relaunches the initiative and holds it again only if the
next runs also end the same way.

A release is refused while a run of the initiative is live, since that run
could still record attempts below the mark. The refusal names the run and the
command that stops it, `cox runs stop <run>`. `cox route edit <task> --initiative
<initiative> --state ready` on a blocked or quarantined task is refused the same way.

The chair does not judge an initiative while its newest run is still going.
If the hold is cleared some other way, without a release, the chair
relaunches the initiative and holds it again only after a newer run ends the
same way.

### A held task

A held task is in state `blocked`, and its row in the store carries a reason
that starts with `needs you:`. You see it in three places:

- The status line lists it under `needs chair` on every tick, as
  `needs chair: <initiative>:held <task>`.
- The dash feed shows the task as `blocked`, with the reason.
- The inbox gets one `needs_attention` entry on the courier for each hold
  reason.

To release the task, edit its ticket if you need to, then set it back to
ready. To give it up, drop it:

```sh
cox route edit <task> --initiative <initiative> --body-file <file> --state ready
cox route edit <task> --initiative <initiative> --state dropped
```

A released task does not go to triage first. If it fails again and its
count is still at the limit, the chair holds it again.

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

### The stall alarm

Each tick, the chair checks these things, each a sign that the loop has stopped moving:

- **No land.** Initiatives are open, and nothing has landed for
  `land_hours`. An initiative is open when its landing record is neither
  `landed` nor `draft` and it is not held for a person. A phase merged into
  its initiative branch counts as a land, and so does an initiative landed
  on its parent. The clock starts at the latest of three times: the last
  land, the oldest open initiative's creation, and the chair's first
  recorded action.
- **No launch.** Lanes are free and work is ready, but no launch has
  completed for `launch_minutes`. If nothing has ever launched, the clock
  starts at the chair's first recorded action. A planned launch does not
  count, so a launch that is refused every tick does not hide the stall.
  This check stays quiet while the chair's own idle-lanes line
  (`idle_stall`, `idle_ready`) is raised for the same condition.
- **The same failure.** One action on one target failed or was refused for
  the same reason `repeat_ticks` times in a row, and the last attempt was
  less than an hour ago. Numbers in the reason are ignored, so a changing
  PR number or count does not hide a repeat. The alarm shows the newest
  attempt's own reason.

- **Idle with work held.** No lane is live and no land is in progress, and
  the chair has made no progress for `idle_minutes`, 60 by default.
  Progress is a launch or land that went out, or a model call. The clock
  starts at the last progress, or at the chair's first recorded action if
  that is later. After a restart, if the last progress is older than the
  setting, the alarm is raised on the first tick. If work is held for you,
  the alarm lists each held task and initiative, its hold reason, and the
  command that releases it. If nothing is held and no host can take epics,
  the chair writes one `no worker capacity` notice instead. That happens
  when every host is draining or none is configured. Being idle on purpose
  is not a stall. Ready work with free lanes is left to the no-launch
  check. You hear about each idle spell once, however long it lasts and
  however its holds change. A live lane pauses the spell but does not end
  it, so a run that dies before its first model call does not start a new
  one. Only progress ends a spell. Its alarm then closes without a message.

Under a hard stop, nothing launches or lands on purpose, so only the
same-failure check runs. A no-land or no-launch alarm that is already
raised is neither raised again nor cleared until the stop lifts.

When a stall starts, the chair writes one `stall_raised` courier entry and
pushes one `stall` notification. When the stall ends, it writes one
`stall_cleared` entry and pushes one `all_clear`. A same-failure stall
ends only when its streak breaks: the action succeeds, or fails for
another reason. If the chair stops attempting the action and an hour
passes with no attempt, the stall expires instead. It is closed without a
message, because nothing showed that the failure was fixed. The alarm
records its own actions, and the next tick reads them back. These rows are
kept at any age, so a stall is never raised twice while it stands, however
long it lasts.

```yaml
chair:
  stall_alarm:
    land_hours: 6       # 0 turns the check off
    launch_minutes: 60  # 0 turns the check off
    repeat_ticks: 3     # at least 2; 0 turns the check off
    stranded_minutes: 30  # 0 turns the check off
    idle_minutes: 60    # 0 turns the check off
```

`chair: idle_alarm_minutes: 60` sets the idle check on its own, as a flat
key. If both are set, the flat key wins.

## What it records

Some actions the loop only records, such as a "needs the chair" report.
Each one is appended to `chair.actions.jsonl` in the runs directory. The
line carries its time and the lease epoch. When the profile names a store,
the same line is also written to the store's `chair_actions` table, so any
machine can read it.
