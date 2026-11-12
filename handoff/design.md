# Landing model: design

Status: proposal for the operator, 2026-11-12. Nothing here is built. Code references are to `~/repos/coxswain` main at `6fe66303d`. The full file:line list is in Appendix A.

## 0. The short version

- **There is no real initiative branch today.** There are two half-built ones (`pr/<initiative>`, made only by the steward, and `pr/<initiative>--<last phase>`, made by the land), and they do not feed each other. That is the root of the stall.
- **Ticket landing is all-or-nothing and waits on a component that is switched off.** Every phase is held until the steward says "epic complete". The steward is off. Even when it is on, one quarantined task stops the whole initiative for good.
- **Proposal:** one initiative branch, created when the initiative starts. A phase is complete when it merges into that branch locally. That merge is done by a separate landing worker, minutes after the phase is built, and it frees the phase's files. The initiative opens its one PR only when it is whole.
- **Landing becomes its own workflow** with its own queue, cap and leases. The build tick stops planning lands.
- **The work setup keeps exactly one PR per initiative.** Its config does not change. Section 5.5 says how that is verified.

## 1. Today, precisely

### 1.1 Branches as they are

```
origin/main  (or the resolved target branch)
 |
 |-- epic/<initiative>/<phase>              phase branch. Cut from main, from a parent phase,
 |    |                                     or from pr/<initiative> if that exists.
 |    |-- epic/<initiative>/<phase>--<task> task branch, merged into the phase by the harness
 |    `-- agents/<run>/<task>               scratch branch for one build
 |
 |-- pr/<initiative>                        "ticket branch". Local only. Made and merged into
 |                                          ONLY by the steward. Never the head of a PR.
 |
 `-- pr/<initiative>--<phase>               the branch a PR is really opened from: a squash of
                                            ONE phase branch onto fresh main
```

In `~/repos/coxswain` there are 1,198 `epic/` refs: phase branches and task branches, nothing else. "Epic" in branch names means one initiative. The real epic (a group of initiatives) has no branch; it only supplies a `target_branch` name.

### 1.2 What the words mean today

| Word | In the store | In git |
|---|---|---|
| approved | task state `approved`, `merged_commit` set | task is merged into its phase branch |
| merged | flag on an approved row | **overloaded.** The harness uses it for "on the phase branch". The land uses it for "on the pr branch". Steer reads it as "holds no files". |
| done | task state `done`. Only a land writes it. | the work is on the target branch on origin, as a squash under a new sha |
| landed | `done` at the configured `land_at` | same |
| phase done / complete | not stored. Derived each tick: every task is approved, done or dropped | nothing. No merge is implied. |
| epic complete | a steward event keyed by the head of `pr/<initiative>` | all phases merged into `pr/<initiative>` and the gate passed there |

Phases, waves, initiatives and epics have no stored state. Only tasks do.

### 1.3 Who merges what, and when

| Step | Who | When | Works? |
|---|---|---|---|
| task -> phase branch | harness, inside the run | at approval | yes |
| phase -> `pr/<initiative>` | steward `merge_level` | first unmerged phase in name order, once all its tasks are approved or done | steward is off since 11-11 |
| gate on `pr/<initiative>`, then "epic complete" | steward | after every phase has merged | never reached |
| last phase -> PR -> main | chair plans it, `cox runs land --phase` runs it | only after "epic complete", only the phase whose name sorts last | never reached |

The PR is opened by the land's `pr_create` step, from `pr/<initiative>--<phase>`, against the target branch. Its body carries no issue line: phase-mode lands never write one.

Two findings from reading the code that matter most:

1. **The single-PR-branch plan is not connected.** `land.py` has a plan that merges each phase into `pr/<initiative>` and opens the one PR from it (`_ticket_branch_plan`). It runs only when the caller passes `phases`. `cox runs land` never passes it. So the PR is a squash of the last phase's branch alone. That is correct only when the phases form one straight stack, so the last phase contains all the others. Sibling phases of a wave are not in it. Their tasks get the note "work not in the merged PR; left in current state" and stay approved.
2. **Two actors each think the other finishes the job.** The steward merges phases into `pr/<initiative>` and gates it. The land ignores that branch and squashes a phase branch. The chair waits for the steward's event before it lets the land run.

### 1.4 The operator's questions

**Is there an initiative branch today?** No, not as a real thing. `pr/<initiative>` is the closest. It is created late, by the steward only, is not pushed, and is not the PR head. Once it exists, later stacked phases are cut from it.

**Is a mixed phase (some tasks approved, some done) a stale fork? Is it refreshed before merging?** Yes, it is a stale fork. "Done" work reached main as a squash under a new sha, and the phase branch still carries the originals beside the unlanded approvals. It is refreshed only when a new *build run* opens that phase (a forward merge since #1138, guarded so a recorded merge is never dropped). It is **not** refreshed before a land. The land squashes the stale branch onto fresh main. A conflict there is recorded and stands until a new run id appears.

**Does the initiative go to main locally or as a stack?** Neither. It goes as one squash PR on origin, built from the last phase. Nothing merges to main locally. There is no stack at `ticket`. (`land_at: level` opens stacked per-phase PRs. That is the shape the work setup must never get.)

**What sets merge order?** Three rules that do not agree:
- the steward: natural phase-name order, one phase per tick;
- the chair's land planner: task `needs` in dependency order, lands of one repo kept together, ties by input order;
- "last phase": plain string max of the phase names, while the CLI uses natural order. `p10` and `p9` would disagree.

**Do approved tasks have their own queue?** No. They are a list of facts rebuilt every tick and regrouped by phase. Held phases are dropped from the plan silently, with no entry anywhere that says "waiting to land since X".

**Do build and merge resources block each other?** Yes, in four ways:
- one tick plans both. Lands do run as detached processes, but they are only started and collected on the tick (3-4 minutes);
- an initiative with a land planned, in flight or backing off is withheld from launches;
- approved work that is not flagged `merged` holds its files in the overlap rule, so other initiatives defer behind it. With the steward off nothing sets that flag, so every approval held its files indefinitely. This is the 20-initiative pile-up;
- one land per repo at a time (a store lease, `land_limit` default 1), with CI waits inside it.

An initiative with any quarantine is also withheld from new launches.

**Why do 10 approved phases of aim-slack not land while the 11th is quarantined? Where does the quarantine go?** Four locks, each enough on its own:
- every ticket phase is held until "epic complete";
- "epic complete" comes only from the steward's gate, and the steward is off;
- with the steward on, a quarantined task makes it publish `needs_attention` once and then do nothing, so the gate never runs;
- the only phase ever let through is the last by name. That is `p6-work`, the quarantined one. The land itself also refuses any phase with a quarantined task.

The quarantine goes to recovery: one relaunch for a triaged cause, then `needs_chair`, which is a notification. After that nobody owns it. In this case the task is a human gate ("start only after an admin approves"). It can never build unattended. Planning put it in the same initiative as buildable work, and landing has no way to leave it behind.

**Sweep or per-initiative agents?** Today it is a global sweep: one tick re-derives everything. See 3.7 for the recommendation.

## 2. Target model

### 2.1 Topology

```
parent                      main | develop | release/x | an epic branch
 |
 `-- pr/<initiative>        INITIATIVE BRANCH. Cut from the parent when the initiative starts.
      |                     The head of the one PR. Recorded in the store.
      |
      |   wave 1            phases with no unmerged parent phase
      |-- epic/<initiative>/p1 --.
      |-- epic/<initiative>/p2 --+-- each merges back into pr/<initiative> when built
      |                          |   (local merge = phase complete)
      |   == wave 1 green ==     '   all wave-1 phases merged, checks pass at that tip
      |
      |   wave 2            cut from the initiative branch AFTER their parents merged
      |-- epic/<initiative>/p3
      |    |-- ...p3--<task>     task worktrees on the phase branch; the only place builds happen
      |    `-- ...p3--<task>
      |   == wave 2 green ==
      |
      `== assembled ==      every phase merged -> refresh onto parent -> push -> ONE PR -> parent
```

Decisions in this picture:

- **The initiative branch is `pr/<initiative>`.** The name already exists, the steward already merges into it, the harness already cuts later phases from it, and the land already has an unused plan that opens the PR from it. No new namespace, and no clash with `epic/<initiative>/<phase>` (git cannot hold both `x` and `x/y`).
- **Phases always fork from the initiative branch.** A phase starts only when every phase it needs has merged into the initiative branch. This removes multi-parent assembly and the "phase built from the wrong base" failure found on the work Mac. Cost: a child phase waits for its whole parent phase, not just the one task it needs. See risk R2.
- **A wave is a label, not a branch.** A wave is the set of phases at one depth. It is green when all its phases have merged and the level checks pass at that tip of the initiative branch. The tip is recorded. A wave branch would add a merge layer and nothing else.
- **Phase and task branch names do not change.**

### 2.2 State machines

A new **landing record** per initiative lives in the store: initiative branch, resolved parent, base sha, and per phase its state, tip and merge sha. This is the "one local record of each phase's tip". Git stays the source for content; the record is the source for intent.

**Task** (states unchanged; one meaning fixed)

| State | Meaning | Moved by |
|---|---|---|
| ready -> building -> approved | built, reviewed, merged into its phase branch, `merged_commit` set | harness (today's rollup) |
| approved + `merged` | its phase is merged into the initiative branch. **Only meaning of the flag from now on.** Holds no files. | lander |
| done | the initiative PR merged into the parent | lander |
| quarantined / dropped | as today | harness, chair |

`done` keeps meaning "on the parent on origin". Cross-initiative needs, issue closing and branch cleanup rely on it.

**Phase** (new, stored in the landing record)

| State | Meaning | Moved by |
|---|---|---|
| waiting | a parent phase has not merged | derived |
| open | branch cut from the initiative tip; tasks building | chair launches, harness builds |
| built | every task approved or dropped, phase validated | harness, on run exit |
| queued | in the land queue | event "phase built" |
| **merged** | merged into the initiative branch. **This is "phase complete".** | lander |
| conflicted | refresh or merge conflicted, or checks failed after the merge | lander, which sends it back to build |
| blocked | a task is quarantined and repair is exhausted | chair recovery |

**Wave**: `open -> green`. Derived from its phases; the green tip is stored.

**Initiative**

| State | Meaning | Moved by |
|---|---|---|
| draft | decomposed, not approved to build | planning |
| building | at least one phase not merged | chair + harness |
| assembled | every phase merged (all waves green) | lander |
| pr_open | refreshed onto parent, pushed, PR open, checks running | lander |
| landed | PR merged; tasks done; branches cleaned | lander (or a person, where the gate is manual) |
| conflicted / blocked | sent back to build / waiting on policy (3.6) | lander |

**Epic**: `open -> complete` (all member initiatives landed). With stacking on (2.4): `open -> integrating -> complete -> landed`.

### 2.3 The one PR

- **Opens when** the initiative is assembled and the gate checks pass on the refreshed initiative branch. Not before. No draft PR at the first phase. (The unused plan opens a draft early; that goes.)
- **Head** `pr/<initiative>`. **Base** the resolved parent.
- **Contains** every phase's merges plus one forward merge of the parent. Merged by squash, so the parent gets one commit per initiative.
- **Title** the initiative's title. **Body** the summary, a block per phase and task (today's body builder), and the reference line.
- **Issue or ticket.** The initiative carries `issue:` or `ticket:` in `initiative.md`, copied from its intake at decomposition. The PR body says `Closes #n` for an issue, `Part of #n` for the epic's issue when only epics are tracked, or the ticket key through a cartridge template (`landing_areas.pr_reference`). `pr_reference` already does this for task lands; phase-mode lands never called it.
- **Initiative branch on origin.** Pushed early only where lane hosts need it. On a single-machine setup it is pushed when the PR opens (`landing_areas.push_initiative: at_pr | early`).

### 2.4 Epics as GitHub stacks (opt-in)

Default: an epic is a group. Its initiatives land on the resolved target one by one. No epic branch. Nothing changes.

With `landing_areas.epic_stack: true` (cartridge, repo overlay, or the epic file):

```
epic's parent (main | develop | release/x)
 `-- <epic target_branch>                 the epic branch
      |-- pr/<initiative-A>   PR #1  base: epic branch
      |    `-- pr/<initiative-B>   PR #2  base: pr/<initiative-A>   (B needs A)
      `-- pr/<initiative-C>   PR #3  base: epic branch              (independent)
```

- An initiative that needs an unlanded initiative of the same epic forks from that initiative's branch, and its PR is based on it. That is the stack. Its layers are initiative PRs, never phases.
- Independent initiatives base on the epic branch.
- When A lands, B merges the epic branch forward and its PR is retargeted to the epic branch. No rebase.
- When every member has landed, the epic branch gets one PR to its parent.
- Per-phase PRs do not exist in any configuration.

### 2.5 Choosing the parent

Unchanged order: the initiative's `target_branch`, else its epic's, else the cartridge's `landing_areas.target_branch`, else the repo default. A team on develop sets it once in the cartridge. A release fix sets it on the initiative or the epic.

New: the resolved parent is written to the landing record when the initiative branch is cut. It no longer floats. Changing it is an explicit edit, which queues a refresh onto the new parent.

## 3. Landing as its own workflow

### 3.1 Queue, owner, cap, leases

One durable **land queue** in the store, with two kinds of item:

| Item | Does | Cost | Lease | Cap |
|---|---|---|---|---|
| `phase_merge` | phase -> initiative branch, local | git plus level checks | `land:<repo>:<initiative>` | `landing.phase_cap`, default 2 |
| `initiative_land` | initiative -> parent: refresh, checks, push, PR, CI wait, merge, bookkeeping | network, minutes | `land:<repo>:<parent>`, limit = today's `land_limit` | default 1 per parent branch |

Each item records why it was queued, when, attempts and the last error. "Approved and waiting since X" becomes visible, and the stall alarm can read queue age.

**Owner:** a **lander** worker. It is a deterministic script, not an agent, and runs as its own loop beside the chair. It claims an item under the lease, performs it, writes the outcome. A claimed item whose worker dies is reclaimed once the lease lapses (today's dead-pid takeover).

Phase merges of different initiatives run in parallel: they touch different branches. Initiative lands are serial per parent branch, since each needs an up-to-date parent. Batching disjoint initiatives (the merge-queue work) stays a later optimisation of that second queue.

### 3.2 Decoupling from the build tick

- Items are enqueued by events: "run exited with a built phase" and "last phase merged". The lander wakes on the event, with a 30 s poll as a floor.
- The chair tick **stops planning lands**. `ticket_held`, `_let_through`, the steward's `merge_level` and gate, and the land half of the tick go.
- The tick reads the landing record as facts only. Its one use: a phase becomes placeable when its parent phases are `merged`.
- Launch caps count builds only. Land caps count lands only. They share CPU (checks), nothing else.
- "Its repo is landing" and "land backoff" stop withholding launches. A phase merge never blocks building in other phases. An initiative land runs only once the initiative has nothing left to build.
- Files are released at `merged`. From then on the lander owns reconciliation, so builds elsewhere do not wait on a PR. Paths in the steer `serial` list stay serial.
- A slow reconcile sweep (15 minutes) enqueues what an event missed and reports drift. It never does work itself.

This is the land queue of the event-driven chair design, built first. That design's other queues can follow the same pattern.

### 3.3 Conflict -> back to build, build -> back to plan

The lander never resolves a conflict and never edits code.

| What failed | The lander does | Then |
|---|---|---|
| merging the initiative tip into a phase conflicts | aborts, sets the phase `conflicted`, files a **rectify task** in that phase naming the paths and both sides | the build workflow builds it like any task (worktree on the phase branch, review, approval). The phase re-enters the queue. |
| level checks fail after a clean merge | same, with the check output as the task body | same |
| merging the parent into the initiative branch conflicts, or gate checks fail | same, in a final `integrate` phase of the initiative | same; the initiative returns to `building` |
| PR checks red | rerun once if the failure is outside the touched component (rule exists); else a rectify task | same |
| a rectify task fails twice | quarantine | escalation, 3.6 |

Build -> plan stays as today and is stated as a rule: a task quarantined for a ticket cause is triaged and amended once, then decomposed again. Landing adds nothing here.

### 3.4 Refresh rules

One rule set, the same for every level. A refresh is always a forward merge of the parent into the child.

| When | What |
|---|---|
| a phase is cut | from the initiative tip. Its parents are already there. No refresh needed. |
| before a task builds | check only: every `merged_commit` the task needs is an ancestor of its base (exists). A gap stops the task; it is never papered over. |
| before a phase merges | merge the initiative tip into the phase (siblings may have merged), run level checks, then merge the phase into the initiative with a merge commit. Record both shas. |
| before the PR opens | fetch; merge the parent into the initiative branch; run gate checks; push. |
| while the PR is open | refresh again only when the forge reports behind-and-required, or a conflict. |

**The guard that never drops a recorded merge** (three parts, two exist):

1. Every refresh goes through `guarded_refresh`: if a recorded sha that was on the branch is no longer an ancestor afterwards, the branch is reset to where it was and the refresh fails. Extended from task merges on a phase to phase merges on the initiative branch.
2. Fetch only ever fast-forwards a local branch; a diverged origin tip is saved on a side ref (#1132).
3. New: before any push, and before any branch delete, every recorded sha that is not yet on the parent must be an ancestor of the tip being kept. No rebase, no force-push, no reset on a branch the record names.

### 3.5 One phase quarantined, others approved (aim-slack)

Under this model the ten built phases each merge into `pr/<initiative>` within minutes of being built. Their tasks read `merged`, their files are free, and the waves show green up to the blocked one. Nothing waits on the eleventh for that.

The initiative is not assembled while a phase is blocked. What happens next is section 3.6.

### 3.6 Quarantine escalation path

| Rung | Owner | Action | Exists |
|---|---|---|---|
| 1 | builder | retry, rescue, check-fix | yes |
| 2 | triage | amend the ticket, one relaunch | yes |
| 3 | planning | decompose the task again | partly |
| 4 | initiative (lander, by policy) | `landing_areas.on_blocked` | new |
| 5 | operator | ntfy with the exact choices | yes, but today it is the only real rung |

Rung 4, `on_blocked`:

- **`split`**: the blocked phase and everything that depends on it move to a follow-up initiative (`<id>-2`, same intake, needs the first). The original is then assembled and lands. Nothing built is lost: a blocked phase's approved tasks stay on its branch and move with it. Allowed only when no merged phase depends on the blocked one.
- **`hold`**: the initiative waits and the operator is told once, with the choices (split, drop, fix). Merged phases still hold no files.

A task marked as a human gate goes straight to rung 4. Planning should also stop producing this: a phase behind a human gate is decomposed as its own held initiative.

### 3.7 Per-initiative owners or a global sweep

**Recommendation: per-initiative ownership of state, deterministic workers, a global sweep only as a backstop.**

- Ownership is a lease and a record, not a standing agent. At most one actor works on an initiative's landing at a time, and the record says who and since when.
- Escalation goes up one chain: task (builder) -> phase (rectify task) -> initiative (policy) -> epic (stack order, retarget) -> chair (caps, cross-initiative) -> operator.
- Models are called only where judgment is needed: building a rectify task and triage. Merging, refreshing, ordering and bookkeeping are scripts.
- A long-lived model agent per initiative is not recommended. Thirty-two open initiatives would mean thirty-two agents mostly idle, each a new way to stall silently, and the steward (78 claimed, none launched) was this shape.
- The global sweep remains as the reconciler in 3.2. It finds what an event missed. It is not the mover.

## 4. What `land_at` becomes

`land_at` answers one question: **which level opens a PR on origin.** Everything below that level completes locally.

| Today | Becomes | Notes |
|---|---|---|
| `ticket` | `initiative` (default). `ticket` stays a permanent alias. | No config or `initiative.md` needs editing. Behaviour: one PR per initiative, phases merged locally. |
| `task` | kept as is | One PR per task against the target. The path that carried hundreds of PRs. It is simple, separate, and the escape hatch. |
| `level` | retired | Per-phase PRs are the shape that must never appear. A config that says `level` is accepted with a warning and treated as `initiative`. |

New keys, all under `landing_areas`, all with defaults that reproduce today's work behaviour:

| Key | Values | Default |
|---|---|---|
| `epic_stack` | true / false | false |
| `on_blocked` | hold / split | hold |
| `push_initiative` | at_pr / early | at_pr |
| `pr_reference` | template for the issue or ticket line | the existing issue rule |
| `phase_cap` | int | 2 |

| | Personal cartridge | Work cartridge |
|---|---|---|
| `land_at` | initiative | unchanged (`ticket` or absent) |
| who merges the PR | lander, on green | a person (today's gate) |
| `epic_stack` | off; on per epic if wanted | off; on per repo only by choice |
| `on_blocked` | split | hold |
| `push_initiative` | early (lane hosts) | at_pr |

## 5. Migration

### 5.1 In-flight initiatives

One idempotent step, **adopt**, run by the lander for every initiative with no landing record. Dry run first; it writes a report per initiative. It works from the store's `merged_commit` and branch ancestry, not from run records, so approvals that live in other runs are included.

For each task it decides, in this order:

| Finding | Action |
|---|---|
| work already on the parent (PR trailer, patch-id, or a merged PR) | mark done |
| approved, commit on its phase branch | keep; the phase is merged as below |
| approved, commit not on the phase branch | carry it (agent branch or stored patch; rule exists) |
| cannot be found or does not apply | back to ready, with the reason |

Then per initiative:

| Shape | Example | Steps |
|---|---|---|
| straight stack, nothing landed | aim-slack | keep or cut `pr/<initiative>` from the parent; merge built phases in dependency order (each later merge is cumulative); flag tasks `merged`; apply `on_blocked` to the quarantined phase |
| phases already landed by phase or batch, one phase mixed | the token-setup initiative | cut `pr/<initiative>` from the current parent tip (it holds the landed phases); merge that tip forward into the mixed phase (guarded); done tasks stay done; the ready task builds on the refreshed branch; the PR carries only what is left |
| one approved task left | the graphs-tests initiative | initiative branch = parent + that phase; lands as a small PR |
| everything done | | record `landed`; clean branches |

Rule that makes this safe: no branch is deleted while it holds a recorded commit of a task that is not done and not on the initiative branch.

### 5.2 The steward

The epic steward's merge role ends. Its parts map as follows:

| Steward step | Becomes |
|---|---|
| `merge_level` | a `phase_merge` queue item |
| `run_gate` | the gate checks in `initiative_land` |
| `epic_complete` | the initiative state `assembled` |
| `relaunch`, `stuck` | the chair's own fill and the stall alarm |
| claims | gone; the lease on a queue item replaces them |

`chair.steward` stays false and the code is removed after the 24-hour run. The steward *seat* that reads run records and proposes changes is a different thing and is not affected.

### 5.3 Existing intakes

Files are under `intake/`.

**Superseded by this design** (close, pointing here)

| File | Why |
|---|---|
| 2026-11-11-completed-ticket-landing-epic-lands-from-its.md | lands no longer wait for "epic complete" or the last phase |
| 2026-11-06-steward-merges-split-levels-marks-content.md | steward no longer merges |
| 2026-11-11-rebuilt-task-whose-first-build-is-already-on.md | a steward merge failure; the adopt rules cover the case |
| 2026-11-12-stacked-phase-is-refreshed-onto-its-parent-s.md | phases fork from the initiative branch after parents merge; its checks move into part 1 |
| 2026-11-11-lands-decomposes-and-builds-run-side-by-side.md | already marked superseded by its direct fix; section 3.2 finishes it |
| 2026-11-11-lane-fetch-never-force-overwrites-a-local-phase.md | fixed by #1132; kept as guard 2 |
| 2026-11-11-refreshing-a-phase-branch-onto-main-never-drops.md | fixed by #1138; extended as guard 1 and 3 |
| the `chair-landing-model` task of work/chair-is-event-driven-a-queue-per-kind-of-work | this document is that section |

**Rewritten** (same goal, new shape)

| File | Change |
|---|---|
| 2026-11-11-chair-is-event-driven-a-queue-per-kind-of-work.md | its landing section points here; the land queue is specified here and built first |
| 2026-11-11-build-the-event-driven-chair-queues-per-kind-of.md | the land queue is taken out (built in parts 1-2); the rest stays for 2026.11.10 |
| 2026-11-06-merge-queue-lands-disjoint-approvals-together.md | batches initiative PRs on the `initiative_land` queue, not task approvals; part 3 |
| 2026-11-11-every-land-path-marks-the-tasks-it-carried-done.md | the paths shrink to two (initiative, task); the rule "refuse if the carried tasks are unknown" stays |
| 2026-11-11-approval-is-never-released-for-rebuild-because.md | approvals are never released by a timer; a failed land produces a rectify task. `approval_hold_ticks` goes. |
| 2026-11-11-hold-that-makes-no-progress-is-reported-and-a.md | steward claims drop out; queue-item age replaces approval holds |
| 2026-11-11-failed-land-records-why-and-awaiting-land-is.md | the failure record becomes the queue item's last error; "awaiting land" becomes the phase states `queued` and `merged` |

**Kept as they are**

| File | Note |
|---|---|
| 2026-11-11-ticket-land-that-has-merged-finishes-its.md | bookkeeping after a merge must be re-runnable; part 1 |
| 2026-11-11-task-marked-done-closes-its-other-open-pull.md | housekeeping |
| 2026-11-11-the-chair-tells-the-operator-when-it-stalls.md | reads queue age once the queue exists |
| 2026-11-07-epic-py-carries-an-approved-commit-missing-from.md | the carry rule adopt depends on |
| 2026-11-10-merged-commit-is-checked-against-the-fetched.md, 2026-11-10-rebased-twin-of-a-merged-commit-counts-as.md | checks adopt uses |
| 2026-11-10-approval-hold-release-leaves-ticket-landing.md | harmless until the hold timer is removed |

**Already built, now the base of this design** (no action): 2026-11-05-epics-group-initiatives-and-initiatives-land-on.md, 2026-11-05-ticket-landing-keeps-one-pr-branch-per.md (its land plan gets connected), 2026-11-07-steward-s-merge-step-follows-land-at-landing-a.md, 2026-11-06-phases-of-one-wave-run-as-separate-lanes-placed.md (its 3 ready tasks need a look against 2.1), 2026-11-03-chair-lands-run-beside-the-tick-not-inside-it.md, 2026-11-02-land-repo-lease-admits-two-lands-per-repo-at.md. Also `done/2026-11-06-ticket-landing-no-longer-defers-a-done-phase-to.md`: it sits in `done/` with 2 tasks still ready; this design does what it asked, so drop those tasks.

### 5.4 Order of work

Each part is one release. Until part 1 ships, lands stay by hand.

**Part 1 - 2026.11.8: phases complete locally, the initiative lands from one branch**

| Work | Proof |
|---|---|
| landing record in the store; `merged` gets its one meaning | unit tests of the state tables in 2.2; a test that steer holds nothing for a `merged` task |
| initiative branch cut at first launch; phases cut from it; a phase is placeable only when its parents are `merged` | real-repo test: a two-wave initiative; the wave-2 base contains wave 1 |
| land queue, lander worker, `phase_merge` with refresh-then-merge under the initiative lease | real-repo test: two sibling phases merge in either order; crash test: a claimed item whose worker dies is reclaimed once |
| `initiative_land`: connect the single-branch land plan, add the issue line, bookkeeping re-runnable | fake-forge test: N phases in 3 waves give exactly one `pr_create`, head `pr/<initiative>`, every task done; re-run after a crash past the merge finishes cleanly |
| chair stops planning ticket lands and stops waiting for the steward | planner test: no `land_phase` for an `initiative` row; launches are not withheld by a land |
| adopt, dry run and apply | fixtures for the four shapes in 5.1, built from copies of the three named initiatives; no recorded commit is lost in any of them |
| work-cartridge contract test (5.5) | in CI from this release on |

**Part 2 - 2026.11.9: conflicts and blocks heal without a person**

| Work | Proof |
|---|---|
| rectify tasks for conflicts and failed checks at both levels | real-repo test: two phases edit one line; a rectify task is filed, built by a stub builder, and the phase merges |
| `on_blocked` split and hold; human-gate tasks | test on the aim-slack shape: ten phases land, the eleventh moves to a follow-up initiative |
| guard part 3 (pre-push, pre-delete) and the guard on the initiative branch | test: a refresh that would drop a recorded phase merge is refused and the branch is unchanged |
| reconcile sweep; queue age in the stall alarm and status | test: an item with no event is enqueued by the sweep; an old item raises the alarm |
| separate caps for phase merges and initiative lands | planner test: a full build cap does not delay a land, and the reverse |
| steward merge code and `approval_hold_ticks` removed | - |
| **Gate: the 24-hour unattended run**, with real lands through the queue and nothing by hand | the epic's existing done-when |

**Part 3 - 2026.11.10, beside the returning feature work**

| Work | Proof |
|---|---|
| epic stacks, opt-in | fake-forge test: A, B-needs-A, C give three PRs with the bases in 2.4; landing A retargets B without a rebase |
| batching disjoint initiative PRs | the merge-queue tests, moved to initiatives |
| `land_at` rename, `level` retired with a warning | config tests: `ticket` and `initiative` resolve the same; `level` warns |
| the rest of the event-driven chair | its own intake |

### 5.5 Verifying the at-mac setup is unchanged

"Unchanged" means: same config file, exactly one PR per initiative, no per-phase PR, no new branch kinds on origin, a person still merges.

1. **No config edit.** `ticket` and an absent `land_at` both resolve to `initiative`. Every new key defaults to the work behaviour (`hold`, `at_pr`, stack off). A test loads a work-shaped cartridge with none of the new keys and asserts the resolved settings.
2. **Contract test in CI** (from part 1, never removed). Against a fake forge, with a work-shaped cartridge, for one- and multi-wave initiatives and for an epic of two initiatives: exactly one `pr_create` per initiative; no PR whose head or title names a phase; no push of any ref outside `epic/<initiative>/...` and `pr/<initiative>`; no merge without the gate. A change that breaks this cannot merge.
3. **Read-only preflight on the Mac before each upgrade.** `adopt --dry-run` over its store, and a dry-run land plan for every in-flight initiative. Pass means: one PR step per initiative, the same base as before, no task sent back to ready.
4. **Deploy order.** The other three machines first. The 24-hour run passes there. The work Mac is upgraded last, by hand, as today.
5. **First initiative after the upgrade** is watched once: one PR, opened only when whole, merged by a person.

Two visible differences, both for the operator to accept (Q4, Q5): the PR's head branch is `pr/<initiative>` rather than `pr/<initiative>--<last phase>`, and the PR body gains a ticket line.

## 6. Risks and open questions

**Risks**

| # | Risk | Answer |
|---|---|---|
| R1 | Files are released at `merged`, so two initiatives can change one file and meet at the PR. | That is the conflict -> build path, and it is cheaper than the pile-up. `initiatives-sharing-files-can-build-together` already chose this direction. The `serial` list still forces order where it matters. |
| R2 | Phases wait for whole parent phases, so deep initiatives build more slowly. | Accept for parts 1-2: correctness first. Decomposition can keep parent phases small. Revisit with numbers after the 24-hour run. |
| R3 | Adopt misreads an in-flight initiative. | Dry-run report first; the no-delete rule in 5.1; fixtures from the three real shapes. |
| R4 | The lander is a new process that can die quietly. | Leases expire, the sweep re-enqueues, and queue age feeds the stall alarm. |
| R5 | Part 1 is large for one release. | Each row in 5.4 is independently testable. If it must be cut, adopt and `initiative_land` stay and `phase_merge` runs on the tick for one release. |
| R6 | Not verified by running: the claim in 1.3 that the single-branch plan is unreachable comes from reading the call site, not from a dry run (a dry run fetches, and a cleanup agent was landing). | Confirm with one dry run before part 1 starts. |

**Questions, with a recommendation each**

| # | Question | Recommendation |
|---|---|---|
| Q1 | Is `pr/<initiative>` acceptable as the initiative branch name, or do you want a new name (for example `init/<initiative>`)? | Keep `pr/<initiative>`. Zero migration, and four pieces of code already agree on it. |
| Q2 | Wave = label on the initiative branch, not its own branch? | Yes. |
| Q3 | Should a blocked phase be split off automatically? | `split` on personal cartridges (needed for an unattended day). `hold` on work, where a partial PR against a ticket is a person's call. |
| Q4 | On work, may the PR head become `pr/<initiative>`? | Yes, unless something at work keys on the old branch name. If it does, the old name can be kept there. |
| Q5 | Where does the work ticket key come from, and how should the PR show it (title prefix, body line, both)? | Carried from the intake into `initiative.md`; shown through a cartridge template. Needs your format. |
| Q6 | Retire `level`? Is anything using it? | Retire. No cartridge in this workspace sets it. Confirm for the work cartridge. |
| Q7 | Do these parts replace the epic's current .8 and .9 scope or join it? | Join. Part 1 comes first in .8. From the old scope, the stall alarm and crash safety stay; the steward and approval-hold items shrink or go (5.3). |
| Q8 | With `epic_stack` on, how do initiative PRs merge into the epic branch: merge commit or squash? | Merge commit into the epic branch (stacks retarget cleanly); squash from the epic branch to its parent. |
| Q9 | Is opening the PR only when the initiative is whole right for work, or do reviewers want an early draft? | Only when whole, as you described. An early draft can be a later opt-in. |
| Q10 | Default caps: 2 phase merges at once, 1 initiative land per parent branch? | Yes on jarvis (checks are the cost). Tune after the 24-hour run. |

## Appendix A: evidence (file:line)

Paths are relative to `~/repos/coxswain/src`.

**Branch names and bases**
- phase, pr, task and scratch branch names: `graphs/harness/epic.py:573-598`
- phase base: pr branch if it exists and the phase is stacked at ticket, else parent phase, else default ref: `graphs/harness/epic.py:3382-3385`, cut ref `:3413`
- new phase branch cut and assembled from parents: `graphs/harness/epic.py:754-796`; refs a phase must hold: `:808-847`
- task merge into the phase, sha kept, branch published: `graphs/harness/epic.py:5055-5062`; `merged_commit` written: `:5161-5177`, `graphs/harness/store_write.py:189-207`
- `pr/<initiative>` name: `cartridges/core/landing.py:82-83`
- steward creates the ticket branch at the target tip and merges a level into it: `tools/agent_tools/chair_steward_exec.py:85-103`, `:46-78`, `:105-119`
- target branch resolution (initiative, epic, cartridge): `cartridges/core/landing.py:46-57`, `tools/agent_tools/cli.py:12854-12882`

**States**
- `land_at` values and default: `cartridges/core/landing.py:6-7`, resolution `:17-24`
- phase complete, need met, merge ready: `cartridges/core/landing.py:70-73`, `:76-79`, `:86-93`
- chair's "phase done" (siblings approved, done or dropped): `tools/agent_tools/chair_read_approved.py:28-38`
- `merged` in the harness means on the phase stack: `graphs/harness/epic.py:4327-4344`; in the land means on the pr branch: `tools/agent_tools/cli.py:3046-3048`; in steer means holds nothing: `tools/agent_tools/chair_facts.py:651-663`
- only a land writes done: `graphs/harness/epic.py:4327-4329`; ticket-wide marks: `tools/agent_tools/land.py:469-477`
- status vocabulary as documented: `cartridges/docs/design/landing-model.md:145-153`

**The ticket hold**
- epic complete read from the steward's reported set: `tools/agent_tools/chair_plan_land.py:123-128`
- last phase by plain string max: `:131-133`; natural order in the CLI: `tools/agent_tools/cli.py:2329-2334`
- only the last phase, only once complete, only if ready: `chair_plan_land.py:136-140`; all ticket phases held otherwise: `:143-154`
- held phases removed before planning, with no action emitted: `:191-199`
- steward: quarantine publishes attention, merges the first settled unmerged level, and does nothing while blocked: `tools/agent_tools/chair_steward.py:61-85`, `:88-95`; gate then epic_complete: `:98-106`
- steward off: `~/.config/agent-tools/profile.yaml` `chair.steward: false`

**The land**
- single-branch ticket plan (cut, merge_phase, draft PR, ready at last): `tools/agent_tools/land.py:528-615`
- it runs only with `phases`: `land.py:650-658`; the only call site omits `phases`: `tools/agent_tools/cli.py:3961-3968`
- non-last ticket phase returns a note only: `land.py:660-662`
- what actually runs: squash one phase branch onto `pr/<initiative>--<phase>`, PR, merge: `land.py:663-664`, `:677-680`, `:747-761`
- refuses a phase with a quarantined task: `land.py:234-241`
- phase-mode PR bodies carry no issue line: `land.py:834-838`; the task-mode rule to reuse: `land.py:1294-1302`
- level PRs stacked on the parent level's PR: `graphs/harness/epic.py:4374-4390`
- one land per repo: `tools/agent_tools/land_repo_lease.py:1-18`, `tools/agent_tools/land_limit.py:18`, acquired at `cli.py:4133-4136`
- lands run as detached processes started from the tick: `tools/agent_tools/chair_land_sink.py:1-6`
- standing conflict keyed by run id: `tools/agent_tools/chair_land_conflict.py:16-22`

**Tick coupling**
- one planner for lands, steward, recovery and launches: `tools/agent_tools/chair_plan.py:1126-1320`
- launches withheld for "its repo is landing", "land backoff", "quarantined", "the steward holds it": `chair_plan.py:1229-1247`; landing set: `:349-352`
- land order by needs, repo-contiguous: `tools/agent_tools/chair_plan_land.py:20-55`; parent wait for non-ticket: `:157-183`
- overlap rule and escalation at three deferrals: `tools/agent_tools/chair_steer.py:73-113`
- scheduler placeable fix (#1139): `tools/agent_tools/chair_schedule_graph.py:93-111`, `:114-132`
- approval-hold release and what it spares: `tools/agent_tools/chair_approval_hold.py:20-29`

**Refresh and fetch**
- refresh is a merge, never a rebase: `graphs/harness/stack_refresh.py:1-6`, `:42-66`
- guard that refuses to drop a recorded merge: `stack_refresh.py:79-102`
- refresh happens when a run opens a reused phase branch at ticket: `graphs/harness/epic.py:1032-1073`, called at `:3563-3569`
- mixed done and approved commits classed as "rebase": `graphs/harness/epic.py:893-909`, handled at `:3489-3499`
- base must hold the merges a task needs: `graphs/harness/epic.py:3346-3359`
- fetch is forward only; divergence saved on a side ref: `graphs/harness/phase_remote.py:71-77`, `:94-130`

**Quarantine**
- recovery kinds; triaged causes get one relaunch, then the chair: `tools/agent_tools/chair_plan_recover.py:26-62`

**Observed in the repos (2026-11-12)**
- `~/repos/coxswain`: 1,198 `epic/` refs, all phase or task branches; PR branches named `pr/<initiative>--<phase>`; recent main commits are one squash per phase (`#1141`, `#1143`, `#1144`).
- `~/repos/aim-slack`: local `pr/<initiative>` holds merges of p1, p2 and p3 only (dated 11-11, the day the steward was turned off). `p5-slack-e` contains every earlier phase, so the stack is straight. `p6-work` has no commits of its own; its one task is quarantined and is a human gate.
- workspace `work/`: aim-slack has 17 approved tasks in 10 phases and 1 quarantined; the token-setup initiative has p1 and p3 done, p2 with one done, one approved and one ready; the graphs-tests initiative has one approved task left.

## Operator answers (2026-11-12)

1. Branch name: `init/<id>` by default, and configurable per cartridge as a template. The work cartridge uses `feature/<issue #>-<name>` for changes to develop and `bug/<issue #>-<name>` or `fix/<issue #>-<name>` for changes straight to main.
2. Blocked phases: auto-split on personal, hold on work. Configurable.
3. The work PR head follows the branch template (see 1).
4. Ticket key: part of the branch template. One per-cartridge template sets the branch name, the PR title and the PR body link together, all from the same issue key. The work cartridge uses `feature/<issue #>-<name>` (or `bug/` / `fix/` straight to main), with `Closes #<issue #>` in the body. The personal cartridge uses `init/<id>`, linked to its epic's issue.
5. `land_at: level`: retire it. This design supersedes it.
6. These parts replace the current .8/.9 scope.
7. Initiative into epic: merge commits, using GitHub stacks to make review easier. Squash into main.
8. Phases wait for their whole parent phase: accepted, but the design should suggest throughput improvements.
9. Conflict handling must not throw away completed or partial work. A rectify or rebuild works from the existing state (the merged phase tip, or the task's existing worktree), with the reviewer's notes. It never regenerates finished work from scratch. Today a rebuild clears the worktree and passes only the reasons and a truncated reference patch (`src/graphs/harness/epic.py` ~215-235, ~723).
