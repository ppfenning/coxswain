# Landing

This page settles how a run's change reaches a person: every governance
path ends the same way, on a branch, as a pull request, merged only on
green.

## Every path ends on a branch

A run's work happens in a worktree that belongs to that run alone. What
leaves the worktree is a diff, applied to a scratch branch — never
committed by the run directly onto a branch a human or another run is
also using. The scratch branch is disposable in the same way the
worktree is: a run that produced something wrong costs nothing to throw
away before a human ever sees it.

## Phase branches collect a phase's tasks

Where a graph's tasks belong to an ordered phase, their scratch branches
land on a shared phase branch rather than each opening its own pull
request against the initiative's target branch. This keeps the phase reviewable
as one unit instead of as however many tasks happened to be in flight.

## Checks are evidence

A build's tests are not something the build gets to assert. The harness
runs the checks itself, in the run's worktree, after it applies the patch.
Each command's exit code and output become an evidence row on the
proposal, before review and the gate see it.

Two lists make up the checks:

1. The team cartridge's `landing_areas.checks`: a list of `{name, cmd}`
   entries. An overlay's list replaces the one it inherits.
2. The repository's own root `.agent-checks` file, if it has one. These
   run on top of the cartridge's checks, never instead of them. A command
   the cartridge already runs is not run twice.

`.agent-checks` holds one shell command per line. Blank lines and lines
starting with `#` are skipped. Each line runs through a shell from the
repository root, so `cd <dir> && <cmd>` works. The check's name is the
line's first word.

A line may end `# fix: <command>`. The shell reads the whole line as a
comment after the check, so the check itself is unchanged. The build runs
the fix command first, before the checks, and keeps what it changed in the
task's commit. A formatter is the usual case, so a style-only difference no
longer quarantines a task:

```text
cargo fmt --check  # fix: cargo fmt
```

The coxswain repository uses this to run every component suite. Its
`.agent-checks` is:

```text
cd src/tools && pytest -q -n auto
cd src/graphs && pytest -q -n auto
cd src/cartridges && pytest -q -n auto
```

Each suite runs from its own folder. Its `pytest.ini` puts the sibling
source folders on the path (`pythonpath = . ../graphs ../cartridges` in
`src/tools`, and the same pattern in the others). So a change to
`src/graphs` in a worktree is the code `src/tools`' tests import, not an
installed copy.

When the chair carries a stranded or split phase onto one `pr/carry-...`
branch, it runs the same `.agent-checks` lines on that branch before it
pushes it.

## Pull requests are where a person decides

From a branch, the change becomes a pull request. `cox runs land <run>
--repo R --apply` cherry-picks the approved work onto a fresh branch off
the default branch and opens the pull request. When the gate allows a
merge, it waits for the checks and merges on green. The chair loop runs
the same land.

How far a land goes without a person is the team's `gate` policy:

| `gate` | Where a land stops |
| --- | --- |
| `ticket` (default) | After it opens the pull request. A person merges it. |
| `phase` | It merges tickets into their phase branch, then stops before the default branch. |
| `epic` | The same as `phase`, until every phase is done. |
| `full` | It merges to the default branch on green. |

The level comes from the team cartridge, resolved into
`<runs>/policy.gate.json`. `--gate` overrides it for one land. An
unreadable or unknown level reads as `ticket`, never `full`.

## Landing granularity

The `land_at` setting answers one question: which level opens a pull request on origin. Everything below that level
completes locally. Its values are `initiative` and `task`. The default is `initiative`. `ticket` is a permanent alias
of `initiative`, so no cartridge or `initiative.md` needs editing. `level` (one pull request per phase) is retired:
it is still read, with a warning, and treated as `initiative`. Any other value is refused with the place it came from.

`land_at` is set in three places, and the narrowest one present wins:

1. The team cartridge, as `landing_areas.land_at`.
2. The repo overlay `.agent/cartridge.yaml`, with the same key.
3. One initiative, as `land_at:` in the frontmatter of its `initiative.md`. It overrides the cartridge and the overlay.

| `land_at` | Pull requests | When one opens |
| --- | --- | --- |
| `initiative` | One per initiative. No phase and no task ever gets one. | When every phase has merged into the initiative branch and the gate checks pass there. |
| `task` | One per task. | After the task is merged into its phase branch. |

### The initiative branch

An initiative has one branch, cut from its parent when its first run starts. Its name comes from a cartridge template,
`landing_areas.initiative_pr.branch`, and defaults to `init/<id>`. The same template block sets the pull request's
title and its issue line, so one issue key names all three. A `pr/<id>` branch an earlier release left is kept.

```
parent (main, develop, a release branch, or an epic branch)
 `-- init/<id>                     the initiative branch: the head of the one pull request
      |-- epic/<id>/<phase>        a phase, cut from the initiative branch once its parent phases merged into it
      |    `-- ...--<task>         a task, merged into its phase when approved
      `-- epic/<id>/<phase>
```

A phase starts only when every phase it needs has merged into the initiative branch. So a phase is always cut from one
branch, and nothing is assembled from several parents. A wave is the set of phases at one depth; it is a label on the
initiative branch, not a branch.

The landing record, one per initiative in the store, names the branch, the parent it was cut from, and each phase's
state, tip and merge commit. The parent is resolved once, when the branch is cut.

Before a run builds anything, it merges the parent's current tip into the initiative branch, so no phase builds on an
old base. A reused phase branch then merges the new initiative tip in, and a new one is cut from it. When the parent
merges cleanly, that is all. When it conflicts, the run files a rectify task in the `integrate` phase, on
`epic/<id>/integrate`, which holds the conflicted merge, markers and all. Every other phase waits, blocked with the
reason and not quarantined, until that task is built and its phase has merged into the initiative branch. The
`integrate` phase needs no other phase, so it merges first, and `on_blocked: split` never moves it to a follow-up
initiative. A merge that fails for another reason (origin has diverged, say) also holds every phase, with the reason,
until a run can take the parent forward. A branch lease another worker holds is not such a failure: the run builds on
the branch as it is and the next run takes the parent forward. A stacked initiative whose lower layer has landed takes
forward that layer's own parent, since the lower branch is deleted; the lander moves the record itself at land time.

### What the words mean

| Word | Meaning |
| --- | --- |
| approved | The task is built, reviewed and merged into its phase branch. |
| merged | The task's phase is merged into the initiative branch. This is "phase complete", and it is the only meaning of the flag at `initiative`. A merged task holds no files. |
| done | The initiative's pull request merged into the parent. Only the lander writes it. |

A merged phase that later gains work (a task sent back to build, a rectify task) is no longer whole on the initiative
branch. It reads by its tasks again until it merges once more.

### The lander

Landing is its own worker, `cox lander run`, beside the chair. The chair's tick plans no land for an initiative. It
reads the landing record as facts: a phase is placeable when its parent phases are merged, and a merged task holds no
files.

The lander works a queue in the store with two kinds of item:

| Item | What it does |
| --- | --- |
| `phase_merge` | Merges one built phase into the initiative branch, after the level checks pass on the merged tree, and pushes the branch to origin. The phase is recorded merged only once origin takes the push; a refused push undoes the merge here and the item is queued again. A build run performs this merge itself the moment it finishes a phase, under the same rule; the lander performs whatever is left queued. |
| `initiative_land` | Merges the parent into the initiative branch, runs the gate checks, pushes, opens the one pull request, reads its checks once per pass until they finish, merges it at gate `full` (or leaves the merge to a person), then marks every task done and cleans the branches. |

Every move of an initiative branch is made under one store lease per initiative, after a fetch, and every phase merge
is pushed before it is recorded, whatever `push_initiative` says, so two hosts never fork it: origin's copy is the one
every host reads. A clone whose copy has diverged from origin's with the same tree (a second "merge phase" commit of
work origin already holds) is reset to origin's, by the lander and by a build run's parent-phase check alike, but
only when origin's tip holds every commit the landing record names: a record written before this rule may name the
clone's own merge commit. A copy that diverged with a different tree, or whose record origin lacks, is left as it is:
the lander holds the initiative, a build run's phase stops with "needs the chair", and either tells the chair once per
pair of commits, naming both. The pushes go through the forge (Forgejo's push waits for the server to show the
branch); when origin still lacks the tip afterwards, as with the local forge, which keeps the branch local, a plain
fast-forward `git push` follows. The two kinds have separate caps (`landing_areas.phase_cap`, default 2, and `landing_areas.land_cap`, default 1)
and neither counts against the launch cap. An item open for an hour is told to the chair once; a pull request waiting
for a person never is. `cox lander status` shows every initiative in flight and every item with its age.

Neither kind of item goes ahead with a branch that carries nothing. Before it merges a phase, the lander asks git what
merging the phase branch would do to its parent (main) and to the initiative branch; before it opens or updates the
initiative's pull request, what merging the initiative branch would do to the parent. A branch that would leave the
tree as it is, while it has approved tasks, is refused with "`<initiative>: no changes against <branch>; the approved
tasks have no commits`": the item waits, and the chair is told once, through needs_chair, which approved tasks have no
commit on the remote. The memory is the store's `lander_notices` (`empty:<initiative>`, with `:<phase>` for a phase),
so the next pass refuses the same way and tells nobody; a branch that carries a change again merges as before, and the
memory is forgotten, as it is once the phase reads merged (a build run may merge it) or the initiative lands, so the
next empty branch is told again. A phase branch the initiative branch already holds is empty unless a merge carried
it in: one off the branch's first-parent line is a merge that was pushed and not yet recorded, and is recorded; one on
that line (a later phase cut from the branch with no commit of its own) is refused. A phase whose tasks were all
dropped, a merge that conflicts, and a branch an open pull request already merged are never refused this way
(`agent_tools/lander_empty.py`).

The refusal fails closed. A merged phase that gains an approved task whose work is already on the initiative branch
(its branch holds only a merge of that branch) is refused like any other, and the chair is told the way out: rebuild
the task (`cox route edit <task> --state ready --rebuild --force`), or drop it (`cox route edit <task> --state
dropped`), after which the phase reads merged again and the notice is forgotten.

The lander never waits inside a pass. A land reads its pull request's checks once: green merges, red goes back to
build, and pending leaves the item `waiting` for the next pass to read again. A failure the forge can rerun is rerun
once per commit, and that pass waits too. The bounds `wait_checks` keeps hold across passes, on the record as
`checks_poll`: a rerun refused five times is final, and right after a rerun the old failures read as pending for at
most five passes. Checks unreadable five passes in a row are not the change's fault but the reading's (a token, the
forge's address), so the land keeps waiting and the chair is told once per commit and reason. A land item that fails
and is queued again keeps its first `queued_at`, so the queue alarm still sees it after an hour. Each pass splits its work
into one lane per repository, and each lane runs on a thread of its own. The caps count per repository. A repository
whose gate checks are still running, or whose lane is still busy from an earlier pass, holds only itself: under
`cox lander run` the other repositories keep landing, and the busy one rejoins the passes once its lane is done. All
the per-initiative work of a pass (the sweep, the queue alarm, reconciliation, the items) runs in the lane that owns
the initiative, so a busy lane's records are not written by anything else in that process.

Each pass also checks every published landing record against origin. Published means assembled, or with a pull
request URL on the record, whatever the state; before that the host that builds the initiative owns its branch, and
a phase it has not pushed yet is not a phase lost. One check runs before publication too, once origin's branch holds
the record: a phase recorded merged whose branch on origin has since moved to a tip the initiative branch does not
hold (the phase was built again after its merge, with every task settled) is merged again at that tip and pushed, and
its tip and merge commit are rewritten on the record once origin takes the push. If that merge conflicts, the
initiative is held and the chair is told once. So an initiative sent back to build (`conflicted`) after its pull
request opened is still judged: its open pull request must still hold every phase merged before, while the
`integrate` work is built on top. Under the initiative's branch lease, the lander fetches origin's initiative branch and reads the
record again. A phase the record calls merged must be on that branch, both its tip and its merge commit. When it is
not and the phase tip is in the clone, or on origin's phase branch, the lander merges the phase into origin's branch
again and pushes it. The push is a plain one, so origin takes it only as a fast-forward, and the open pull request
gets the repair. The record's merge commit is rewritten only after origin takes the push. When the tip is gone, the
initiative is held: `cox lander status` shows the reason, the chair is told once, and nothing merges into the
initiative (a build run's phase merge included) or lands from it. A busy lease, a refused push, an unreadable origin or a
clone that has diverged from origin waits for the next pass and changes nothing. A clone ahead of origin (a repair
merged here whose push never happened) is built on, and the push publishes it. The hold lifts on the first pass that
finds the record and origin agree. Whatever reconciliation decided, the land itself checks once more right before
its merge: while the branch that would merge (origin's, or this clone's for a forge that merges locally) lacks a tip
or merge commit the record names, the land waits.

The record is what decides which phases an initiative carries, not the branch. So do not drop a phase by resetting
`init/<id>` on origin by hand: the next pass reads that as a lost phase and merges it back in (or, when its tip is
gone, holds the initiative). To take a phase out, move it to a follow-up initiative (`landing_areas.on_blocked:
split`, which runs `split_initiative`), or edit the landing record so it no longer calls the phase merged.

Lands on one parent are one at a time by default, since each needs an up-to-date parent. With
`landing_areas.land_batch` above 1, the lander lands together up to that many initiatives that share a repo and a
parent and change no file in common. Each keeps its own pull request. They are refreshed onto the same parent so their
checks run side by side, then merged together in a scratch tree where the gate checks run once for the batch. When
that passes, the pull requests are merged in queue order without refreshing again. When it does not, only the first is
landed and the rest wait. Batching applies only where the lander itself merges (gate `full`); an initiative whose
files overlap another's waits its turn.

The lander never edits code itself. What a phase merge does with a conflict depends on the lane model the landing
record was made under. The record's `lane_model` is the initiative's, stamped when the record is made; a record with
no `lane_model` is on the old model.

- **Old model.** A phase merge that conflicts, or fails its checks, is sent back to build as a rectify task in that
  phase. A conflict with the parent, or a red pull request, becomes a task in a final `integrate` phase. The conflicted
  merge is committed on the branch the task builds on, so nothing already built is redone. A rebuild of any task starts
  from its last attempt, put back in full in a tree of its own, and only the change is built.
- **New model (`lane_model: phase`).** The phase lane never merges into the initiative branch: the lander is the only
  merger. It merges origin's copy of the phase branch, and never writes that branch. The level checks are the phase
  level gate: the cartridge's `level_checks`, then the `# level: phase` section of the merged tree's `.agent-checks`. A
  conflict goes to the merge agent, in the merge's own scratch worktree. The merge agent is the phase lane's own
  (`merge_tasks`, the seat that merges two colliding tasks), run through the local harness with the provider profile's
  runner; it is given the phase's change and each conflicted file with both sides between markers, and returns each
  file's whole merged text. Its whole answer is bounded at 450 s, so the agent, the guards and the level checks fit
  inside the 900 s branch lease; an agent that runs past it is a resolver error. Its answer must leave no conflict
  marker (the diff3 style's `|||||||` included) and must keep every line either side added (against the conflict's
  index stages), or it is refused; then it must pass the same guard and level checks as any merge, and the phase
  records who resolved it (`resolved_by`). A cartridge that binds no `merge_tasks` seat holds as "no merge agent yet".
  If the
  merge cannot be made, the initiative is held and the chair is told; no rectify task is filed, and the lander never
  writes the phase branch. The hold records its phase, the tip it could not merge, its cause and how many times that
  merge was tried. It lifts, and the merge is tried again, in three ways. Origin's branch of that phase moves to a new
  tip (a fix was pushed). An operator releases it with `cox lander release <initiative>`. Or its automatic retry
  comes due: a resolver that raised, or a harness fault, is retried up to three times, 10, 20 and 40 minutes after
  each attempt, and "no merge agent yet" is retried hourly without a bound, so the first pass after a `merge_tasks`
  seat is bound settles it. A clean refusal, a failed level check and a guard refusal are never retried on their own. A retry
  that fails the same way is not told again; a released one, and the last automatic retry, are. The merge agent
  answers in the merge's scratch worktree, and nothing but the conflicted paths may change there: an answer that
  staged or changed anything else, or moved the merge, is refused. Reconciliation never replaces a merge hold: a drift
  it finds is kept beside it and cleared on its own.

Refreshes are always forward merges of the parent into the child. No branch the record names is rebased, reset or
force-pushed, and a merge that would drop a recorded commit is refused. After a land, a phase branch is deleted only
when the landed branch holds its tip.

### A blocked phase

`landing_areas.on_blocked` says what happens when a phase holds a task that is quarantined past repair:

- `hold` (default): the initiative waits and the operator is told once, with the choices. Merged phases still hold no
  files.
- `split`: the blocked phase and every phase that depends on it move to a follow-up initiative, `<id>-2`, with what
  they had built, and the first initiative lands without them. It is refused, and held, when a merged phase depends on
  the blocked one.

### Epics as stacks

The epic steward is gone: nothing but the lander merges a phase or lands an initiative. A profile that still sets
`chair.steward` is read and ignored, and `cox setup doctor` says so.


By default an epic is a group: its initiatives land on the resolved target one by one. With `epic_stack: true` (in the
cartridge, the repo overlay or the epic file) the epic's `target_branch` is an epic branch. An initiative that needs an
unlanded initiative of the same epic forks from that initiative's branch and bases its pull request on it. When the
one below lands, the pull request is retargeted to the epic branch and the epic branch is merged forward. Initiatives
merge into the epic branch with merge commits. When every member has landed, the epic branch gets one pull request to
its parent. The layers of a stack are initiative pull requests, never phases.

### Bringing an in-flight initiative under the record

`cox lander adopt` reports, for each initiative with no landing record, what adopting it would do, and does it with
`--apply`. Work already on the parent is marked done, an approval off its phase branch is carried onto it, one that
cannot be found goes back to ready, the branch is kept or cut, and every built phase is merged. Nothing is deleted, and
an initiative that has a record is skipped, so it is safe to run twice.

### At task

`task` is unchanged: each merged task opens its own pull request against the target branch, phases stack on their
parents' branches, and the chair plans those lands on its tick. A failed pull request is a note on the task. It is not
a quarantine, because the merge into the phase branch already happened.

### Status vocabulary

A task row reads one of these words in the dashboard feed. The words are listed in the order a task moves through them.

| Status | What it means |
| --- | --- |
| `ready` | The task is ready, every task it needs has landed, and no lane is building it. |
| `building` | A live lane covers the task's initiative and phase. The lane covers a phase, so idle siblings read `building` too. |
| `approved` | The work passed review and is on its phase branch. |
| `merged` | At `initiative`, the task's phase is merged into the initiative branch. |
| `landed` | The task is `done`: its pull request merged. |

A task whose needs are not all landed reads `blocked`. A dropped or quarantined task reads as that state.

### Parallelism

A level runs its ready tasks together, up to a cap. The cap is the `--max-parallel` option of the phase and epic
commands. Without it, the cap is the `max_parallel` setting of the provider profile, a positive integer. Without that,
it is the free lane count. A zero, a negative or a non-integer profile value is refused.

Phases form a directed graph of levels. The driver derives each phase's parent phases from the needs between tasks, with
`phase_parents` in `harness/waves.py`. `compute_waves` in the same module sorts the phases into waves. A wave holds the
phases whose parents all sit in earlier waves. A phase caught in a dependency cycle is placed in no wave and is recorded as blocked.

At `initiative` every phase is cut from the initiative branch, which holds each parent phase once that phase has merged
into it. A phase's parents are only the phases its tasks need, so phases with no need between them still run together.
At `task` there is no initiative branch: a phase is stacked on its one parent's branch, or assembled from several.

At launch the driver prints one line per phase, `phase <id>: parents <ids>`, before any phase builds. When a later phase
names a file in `surfaces` that an earlier, unfinished phase also names, and no chain of needs reaches that phase, its
line ends with `not inherited: <phase> (<files>)`. One warning line follows. The phase builds without that earlier work, and
nothing is blocked. A task need on a task in the earlier phase adds it, and the decompose step adds that edge for a shared
file. Only files named in `surfaces` are compared, so a dependency through an import, a symbol or a generated file is not seen.

Independent phases in a wave run together, one lane each. A lane number is reused once its phase finishes. The
`_run_lanes` function in `harness/epic.py` runs the phases as lanes.

Tasks inside a phase run in parallel up to the cap. `lane_bound` in `harness/lanes.py` lowers the number that start when the usage ceiling is
tighter than the lane limit. `plan_launches` in `harness/scheduler.py` uses `next_launches` from `harness/lanes.py` to choose which
ready tasks start. The scheduler holds the ready tasks of a phase while another phase has a build in flight over an overlapping surface.

A phase still waits for its parents. At `initiative` a parent counts for its dependents only once it is merged into the
initiative branch, whole: every task approved (or dropped) and on the phase branch, and the merge recorded. A phase with
a task still ready, building or quarantined reads partial and is not merged, whatever the validator said of its goal, and
no task of a later phase starts early. A phase with several parents waits for all of them. Across hosts, the clone must
hold the recorded merge commit, fetched from origin if need be; a phase that would build without it stops, with no quarantine.

At `task` a parent counts complete when every task in it is done, and a ready task whose own needs are all merged may
start before its parents are complete.

## Target branch

The target branch is the parent branch an initiative lands into. It resolves like `land_at`, and the narrowest value
present wins:

1. The team cartridge, as `landing_areas.target_branch`. This is the default.
2. The epic definition, `epics/<id>.md`, as `target_branch`. It overrides the cartridge for every initiative in that epic.
3. One initiative, as `target_branch:` in the frontmatter of its `initiative.md`. It overrides the epic.

```yaml
landing_areas:
  target_branch: release/next
```

An explicit `--base` flag on a land still wins over all three. Every value given is checked, including one that a
narrower value overrides. A bad value is refused with the place it came from. See [the hierarchy](index.md#the-hierarchy)
for how an initiative joins an epic.

## Merge on green

A pull request merges once its checks are green, not once a run has
asserted that they are. The land polls the forge's own check results for
the pull request. A failed check stops the land; nothing merges. This is
the same evidence-over-assertion rule the review nodes enforce, applied at
the last step: the checks that actually ran are what green means.

## Concurrent lands per repository

By default a repository admits one land at a time. The repo lease holds
that single slot, so a second land of the same repository waits for the
first to finish. The `land_limit` setting raises the number of slots. It
lives in the repo's overlay file, `.agent/cartridge.yaml`, either at the top
level or under `repos.<directory name>`, where the directory name is the
last part of the repo's path:

```
land_limit: 2
```

The value is an integer of at least 1 and the default is 1. A valid value
under `repos.<directory name>` wins over the top-level one. A value that is
not an integer of at least 1 is skipped, so an invalid per-repo value falls
through to a valid top-level value. The limit is 1 only when neither is
valid, or when the file is missing or unreadable. The setting is independent
of `land: manual`.

With a limit N above 1, up to N lands of one repository plan, build and
check at once, each from current main. Only the merge step is serialized.
Each land takes a short per-repo merge lock, named `merge:<repo>` so it
never collides with a land slot, for the state read and for each merge try.
The lock is released before any branch update or wait for checks, because
those take minutes. Its lease lasts two minutes, so a crashed holder frees
it soon, and a land waits up to a minute for it. A land that cannot take
the lock in that time stops and is retried on the next tick. When the wait
timed out, the stop names the holder of the lock. When the store was
unavailable, the stop gives that outage as its reason instead. No merge
lock is taken with a limit of 1, because the repo lease already admits one
land. None is taken either when the repo has no lease, because there is no
store to take it in.

A land whose branch conflicts with a sibling that merged first stops with
that reason: the base moved under a merged sibling, and its pull request now
conflicts with it. Nothing merges for that land. It is retried on the next
tick, and a retry that still conflicts stops with the same reason.

The forge's merge queue was not used. It may be unavailable on a repo owned
by a personal account. It would also run CI twice per land on the single
self-hosted runner.

## Manual repos

A repo whose overlay (`.agent/cartridge.yaml`) sets `land: manual` is one where a person merges the PR. The chair and
`cox runs stranded` propose no `cox runs land` for it; the remedy line says the human merges the PR. Each tick the chair
runs `git fetch origin <branch>` for the PR branches the land builds (`pr/<initiative>--<phase>` and `pr/<task>`), the
base and the phase branch. It then asks whether a PR branch is an ancestor of `origin/<base>` or `origin/<phase>`, or
merges into one of them with no change, which is what a squash merge leaves behind. A merge done on the forge never moves
local refs, so only `origin/` refs count. When the answer is yes the approved task is marked done, through the same
store write as `cox route edit --state done`, and its dependents start. A task whose merge is not yet on origin is
rechecked every five minutes. A PR branch deleted on origin cannot be checked: the chair reports it as failed with the
commands below.

To finish a task by hand, run the merged-branch check first, then the escape hatch:

```
git fetch origin <base> <phase>
git branch -r --merged origin/<base>
cox route edit <task> --initiative <initiative> --state done
```

Look for the PR branch in that list, or for its squash commit in `git log origin/<base>`, before `cox route edit <task>
--initiative <initiative> --state done` is run.

## runs clean

`cox runs clean <run> --repo R --apply` removes the worktree and
branches a finished run left behind. Without `--apply` it shows what it
would delete. It cleans up runs that finished (landed, abandoned or
superseded), so a workspace doesn't accumulate disposable state that nobody is going to look at
again. It never touches a run still in flight or a branch a pull request
still points at.
