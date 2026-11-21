# The event-driven chair

**Status: proposed, not approved.** Nothing on this page is built. It is H6 part 2 of the
[hardening plan](../hardening.md#h6-the-chair-tick-is-cheap-then-event-driven). The plan says the queues are approved
here before any build. It replaces the dropped `chair-is-event-driven…` design, which was written before the lander
existed.

## Why change the shape

Today the chair is a loop. Each tick gathers every fact (`chair_facts.gather_facts`, about 60 readers), plans every kind
of action from all of them (`chair_plan`), performs the plan, and sleeps. `chair_wake` can end the sleep early when a run
exits, intake arrives or a directive lands on the bus, but the next tick still reads and plans everything.

H6 part 1 made that loop cheap. The store readers keep their rows while the store's change counters for their tables
stand (`chair_tick_cache`). The directory, git and parsing readers no longer re-scan the board once per initiative. On
a restored slice of the chair's store and workspace, a quiet dry-run tick went from about 30 s to about 4 s. A cheap loop
still has three costs the loop shape itself causes:

- **Latency is the interval.** A run that exits waits for the next tick, unless the wake catches it. A quarantine
  that needs triage waits too, and so does an approval that could land.
- **One slow kind holds every kind.** A forge read for a land check sits in the same tick as a launch decision. H3 already
  moved lands out of the tick for this reason.
- **Every decision re-derives from everything.** The plan is a pure function of all facts. That is good for tests, but
  a fact that changed for one initiative is planned against every initiative again.

## What the lander already does

The lander (`agent_tools.lander`, `harness.landing_store`) owns landing apart from the tick, and it works like this:

- Items sit in the store's land queue table (`land_queue`, migration 0016). Each has an id built from its key
  (`item_id(kind, initiative, phase)`), so enqueueing the same item again is a no-op.
- An item moves `queued -> claimed -> done | failed`. A claim carries `lease_until`. If the lease lapses, any worker
  may claim the item again, so a worker that dies leaves nothing stuck.
- Every step reads what is already true (the record, the branch, the forge) before it acts, so a step can run twice.
- An item open longer than `QUEUE_ALARM_SECONDS` is told to the chair once.

The proposal gives the chair's other work the same shape.

## The proposal: a queue per kind of work

The chair's actions fall into a few kinds, each with its own inputs and its own pace. Each kind gets a queue and a
worker. A worker claims one item, gathers only that item's facts, decides with the existing pure planner for that kind,
acts, and closes the item.

| Queue | One item is | Fed by | Decides with (today's pure core) |
| --- | --- | --- | --- |
| `launch` | an initiative with ready work and a free lane | work_items change, run exit, lane or host change, timer (pacing window) | `chair_plan` launch and dispatch rules, `chair_facts.dispatch_facts`, pacing |
| `quarantine` | a task newly quarantined or held | work_items change to `quarantined` or `blocked`, run exit with a failure | `chair_remedy`, `chair_plan_recover`, triage |
| `land` | (exists) a phase merge or initiative land | approval, phase merge | the lander |
| `watch` | a land whose main CI is not yet known | land done, timer (poll CI) | `chair_revert_watch`, `chair_revert` |
| `decompose` | an intake to decompose, or an empty decompose to report | intake arrival, decompose run exit | `empty_decompose_facts`, decompose miss rules |
| `housekeeping` | one sweep (stale tickets, stalled runs, idle alarm) | timer only | `chair_stale`, `chair_stall_alarm`, idle stall |

The `land` queue stays as the lander has it. `housekeeping` is the old tick in miniature, kept for facts that only age.
Nothing writes them, so no event can announce them.

### Feeds

An item is enqueued by one of three feeds. Each feed only enqueues; it never decides.

1. **Store changes.** The writers that change work state already go through one API (`harness.store_api`, H1). After a
   committed write, the API enqueues the item that the change makes due, in the same transaction, keyed like
   `land_queue` so a repeat is a no-op. The first version can derive items instead: a feed reads the rows changed since
   a cursor, using `work_items.updated_at`, which needs no trigger. Postgres `LISTEN/NOTIFY` can wake a worker early, but it is only a wake. The queue row is the record, so
   a missed notification costs latency and never loses work.
2. **Run exits.** `run_exited` courier events (today's `chair_wake`) enqueue the run's initiative on `launch`, and on
   `quarantine` when the exit failed. The exit is acknowledged only once the item exists.
3. **Timers.** One small scheduler enqueues `housekeeping` and `watch` polls at their intervals, and a `launch` check when
   the pacing window opens. A timer item has the same idempotent key as any other, so two schedulers enqueue it once.

### Workers

- One process, the chair, runs one worker loop per queue, each a thread, under the existing chair lease. The lease
  still says which machine is the chair. A worker claims items with a lease, as the lander does, so a restarted chair
  picks up where it stopped.
- A worker gathers the facts of its item only. The readers keep their shape: each takes an initiative, or filters to
  one. The stamped reader cache of part 1 stays, so two workers reading the same table between writes share one read.
- Limits that span items stay global and are read under a claim. Examples are lanes in flight, launches per hour and the
  shared-file limit. The `launch` worker claims one item at a time, so two launches never race for the last lane.
- The status line and timing line become one line per worker, plus the queue depths. The stall alarm (H7) reads the queue
  ages: an item older than its kind's bound is a stall.

### What stays the same

- The pure planners and their tests. Each queue calls the part of `chair_plan` that is its kind's today. Splitting
  `plan_tick` into per-kind functions is the main code move, and it is mechanical.
- The action records (`chair_actions`), the courier, the inbox and the lease.
- `cox chair run --once --dry-run`. It drains every queue once, in a fixed order, and prints what each worker would do,
  so the smoke suite and a person can still run one deterministic pass.

## Order of build, once approved

1. Split `plan_tick` into per-kind planners with no behaviour change; the existing tests pass unchanged.
2. Add a `chair_queue` table shaped like `land_queue` (kind, key, state, lease), with a migration that has a finite lock
   wait.
3. Add the `housekeeping` and `watch` queues, fed by timers. They are the least coupled, and they prove the worker loop.
4. Add `quarantine` and `decompose`, fed by run exits and store changes.
5. Add `launch` last. It holds the global limits, and it is where a race would cost most.
6. Remove the tick loop. `--once` stays as the drain.

Each step ships behind the existing tick until the next replaces it: the tick skips a kind whose queue is enabled.

The cutover (C6) is an operator's procedure, on the [chair cutover](../customize/chair-cutover.md) page: the plan's
§5.3 to §5.5 with the commands as built. Two of them differ from the plan's text. The worker is
`cox chair worker --kind <kind>` (the plan wrote `cox chair work`), so §5.4 step 4 is
`cox chair worker --kind housekeeping --once`. And §5.3 step 3's comparison is one command, `cox chair parity`.
Once the tick is stopped, the queue alarms (`queue_unowned`) are raised by the first live worker, and
`cox chair watchdog` checks the worker leases behind them. The stall alarm and the host checks go with housekeeping.

## Open questions

- **Cursor or trigger for store changes.** A cursor on `updated_at` needs every writer to stamp it. Most do, but the
  queue upsert keeps an older `updated_at` on a changed row (see `store_api.stamp_of`). A trigger that enqueues is
  exact, but it puts logic in the database. The proposal starts with the cursor and a full re-derive in `housekeeping`
  as the safety net.
- **One process or several.** Threads in the chair keep one lease and one log. Separate processes, as the lander is,
  isolate a crash. The proposal starts with threads.
- **Priority between kinds.** A `quarantine` triage and a `launch` both spend model budget. Pacing stays one global
  check, read under the claim.

## Done when

- This page is approved.
- After the build: no full fact gather runs on a schedule; a run exit reaches its `launch` or `quarantine` decision
  within seconds, proved by a smoke test that exits a run and times the decision; and every queue's age is on the status
  line.
