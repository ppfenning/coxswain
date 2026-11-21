# Chair cutover

This page moves the chair from the tick loop (`cox chair run`, the unit `coxswain-chair.service`) to the five queue
workers (`cox chair worker --kind <kind>`, one unit each). It also covers how to move back. It is for the operator of
the chair machine. The [autonomous chair](autonomous-chair.md#queue-workers) page explains what each kind does.

Nothing is migrated. The workers read the same store rows as the tick, so the cutover stops one process and starts
five. The rollback reverses that, and the tick needs nothing the workers wrote.

## Before the day

Do these checks before the stop, on any day.

1. **Every machine runs a `cox` with the queue units (C6).** Run `cox chair status` on each one. A `cox` that does not
   know the command is too old. Upgrade every chair and lane machine first, and any other machine whose profile could
   list `chair.queues`.
2. **The watchdog timer is installed on the chair machine.** It is the check behind the workers: with every worker
   down it still pushes `Chair queue down`.

   ```sh
   cox chair watchdog --install      # writes the timer files and prints the lines that activate them
   ```

   Run the lines it prints.
3. **The notifier is set** (`notify: {ntfy: <url>}` in the profile), so a worker's `needs chair` lines reach you.
4. **The plans match on a copy of the real store** (§5.3 step 3). Dump the live store, restore the dump into a
   throwaway database, and compare the tick with the workers against the copy:

   ```sh
   pg_dump -Fc -f /tmp/cox-store.dump "$LIVE_STORE_URL"
   createdb -h <host> -U <owner> cox_parity
   pg_restore -h <host> -U <owner> -d cox_parity --no-owner /tmp/cox-store.dump
   cox chair parity --store-url postgresql://<owner>:<password>@<host>/cox_parity
   ```

   `cox chair parity` plans these, each a dry run that takes no lease and performs nothing:
   - the tick with `chair.queues: []`;
   - the tick with every kind listed;
   - each kind's worker.

   They run through a copy of your profile, in a scratch workspace. The last line is `parity: match` (exit 0), or
   `parity: MISMATCH` followed by what differs (exit 1). The command refuses the store your profile itself names.
   Drop the throwaway database afterwards. Stop on a mismatch: the workers would not do what the tick does.
5. **The store's notifications work.** `cox setup doctor` passes its `store notify queue` row. A worker wakes on a
   store notification and polls every 60 seconds as its fallback.

The plan's §5.3 step 4 asks for a `store_cursor` whose per-table `seq` `cox chair status` would print. That cursor
was not built: the workers wake on the store's `NOTIFY` and poll, so there is no per-table `seq` to watch, and the
`store notify queue` row is the check that stands in its place.

## Preconditions for the stop

All of these, at the moment you start:

- **No old-model lane is live.** The chair is idle by your choice: `cox route status` shows no run in flight on a
  `lane_model: epic` initiative. After the stop, nothing plans the tick's land slot for the old model (the lander
  lands new-model phases).
- **No land is claimed.** `cox lander status` shows no claimed item in the land queue.
- **The store was dumped within the hour.** Keep the dump until the queues have run a day.

## The stop

Five steps. The lander and every lane worker stay up throughout: they do not depend on the tick.

1. **Stop the tick.**

   ```sh
   systemctl --user stop coxswain-chair.service           # Linux
   launchctl bootout gui/$(id -u)/coxswain-chair          # macOS
   ```

2. **List every kind** in the chair machine's profile (`~/.config/agent-tools/profile.yaml`), in the flow form:

   ```yaml
   chair:
     queues: [housekeeping, watch, quarantine, decompose, launch]
   ```

3. **Install and start the five workers.** The same command disables the tick's unit, so it does not come back at the
   next boot or login:

   ```sh
   cox chair service --install --queues --label <the chair's label>
   ```

   Linux gets one systemd template, `~/.config/systemd/user/coxswain-chair@.service`, with five instances
   (`coxswain-chair@housekeeping.service` and the rest), and the tick's unit is disabled. macOS gets one launchd agent
   per kind (`~/Library/LaunchAgents/coxswain-chair-<kind>.plist`), and the tick's agent is booted out and disabled.
   The command refuses, writing nothing, unless `chair.queues` lists all five kinds. It is safe to run again: a file
   that did not change is not rewritten, and a running worker is restarted only when its file changed. Add
   `--dry-run` to see the files and the `systemctl` or `launchctl` steps first. It takes `--environment-file` and
   `--profile` as `cox chair service --install` does.

4. **Catch up on what changed during the stop.**

   ```sh
   cox chair worker --kind housekeeping --once
   ```

   This is one housekeeping pass now. Every pass re-derives its kind's work from the store's current rows, so the
   other kinds catch up on their first pass with no command. While the housekeeping unit already holds
   `chair:housekeeping`, the command stands by and exits 1 ("another worker holds chair:housekeeping"). That means the
   unit's first pass has run or is about to, so there is nothing more to do.

5. **Check.**

   ```sh
   cox chair status
   cox setup doctor
   ```

   `cox chair status` exits 0 when all is well:
   - each `chair:<kind>` line reads `live`, held by that kind's worker, with a beat a few seconds old;
   - the units read `active` on Linux and `loaded` on macOS;
   - there is no `NOT RUNNING` line.

   The chair lease (`chair (the tick's lease)`) reads `expired` or `released` once the tick has stopped. That is
   expected: no worker needs it.

   `cox chair status` prints no queue depth or oldest age, because there is no queue table to measure: each pass
   re-derives its kind's work.

   `cox setup doctor` should pass its required rows. Its `units start` row judges the units an upgrade restarts. After
   the cutover these are the five worker instances and the lander, and the tick's unit, now disabled, is not judged.
   An edge machine's scheduled upgrade restarts the five with the lander.

Then release one small initiative and watch its run's exit reach its decision within seconds. The quarantine and
launch workers wake on the `runs` and `phase_queue` writes.

## What raises the alarms now

The tick used to raise `queue_unowned <kind>` when a listed kind had no live worker. With the tick stopped:

- **the first live worker** in the order housekeeping, watch, quarantine, decompose, launch raises it for every other
  listed kind. Normally that is housekeeping. When housekeeping is the one down, the watch worker raises
  `queue_unowned housekeeping`;
- **the watchdog timer** pushes `Chair queue down` for every listed kind whose lease has been dead longer than
  `--stale-minutes` (10), even with every worker down. With every kind listed, it no longer judges the tick's lease or
  status lines;
- **each worker** pushes its own pass's `needs chair` lines and alarms. The stall alarm and the login and host checks
  went to housekeeping with the cutover.

Two duties of the tick have no worker. Both are covered by the preconditions above:
- the old model's land slot: lands, fetches, carries and closes for `lane_model: epic` initiatives;
- the tick's own loop chores: the workspace pull, the CI-gate pause line and the lane checkout check.

Run no old-model work while the queues are on.

## Rollback

At any point after the stop:

1. **Take every kind out of `chair.queues`:**

   ```yaml
   chair:
     queues: []
   ```

   Each worker sees the change on its next beat and lets its lease go, so the tick can take every kind back.
2. **Stop and remove the workers, and start the tick:**

   ```sh
   cox chair service --uninstall --queues
   ```

   It stops and disables the five units, removes their files, and enables and starts `coxswain-chair.service` (on
   macOS, enables and boots in `coxswain-chair.plist`). It refuses while `chair.queues` still lists a kind, because the
   tick skips every listed kind and would plan nothing for it. That is why step 1 comes first. It is safe to run again.
3. **Check** that `cox chair status` shows the chair lease `live`, held by the tick, and that `cox chair run` status
   lines are coming in again. The `chair:<kind>` lines go to `released` or `expired`.

The tick derives everything from the store's rows on each pass, so it needs nothing the workers wrote. No schema step
and no restore. New-model initiatives in flight keep working either way.

After a rollback the tick's runs directory keeps `chair-queue-lease-seen`, which makes the tick fail closed when it
cannot read the worker leases. Once no worker will run on this machine again, delete it as the
[autonomous chair](autonomous-chair.md#queue-workers) page describes.

The tick loop stays in the tree until the queues have run two weeks without a rollback.
