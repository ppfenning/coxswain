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
request against the epic's target branch. This keeps the phase reviewable
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

The `land_at` setting says at which size of unit a change lands. Its values are `ticket`, `level` and `task`. The
default is `ticket`. An unset value reads as `ticket`, and any other value is refused with the place it came from.

`land_at` is set in three places, and the narrowest one present wins:

1. The team cartridge, as `landing_areas.land_at`.
2. The repository overlay, `.agent/cartridge.yaml`, under the same key. It replaces the team value for that repository.
3. One initiative, as `land_at:` in the frontmatter of its `initiative.md`. It overrides the cartridge and the overlay.

```yaml
landing_areas:
  land_at: level
```

Every value given is checked, including a cartridge value that an initiative overrides, so a bad cartridge never hides.

The approval gate applies only at that granularity. The gate level is the `land_at` value itself. At `ticket` a run
asks nobody about its batch and clears it unless the caller passes an explicit `assume`, because the one pull request
is where a person decides. At `level` and `task` the batch goes through the gate. The team `gate` policy above still
applies, and the earlier stop wins. `ticket` stops at the rung of the one pull request, `level` at the phase rung, and
`task` keeps the team's `gate` value.

| `land_at` | Pull requests | When one opens |
| --- | --- | --- |
| `ticket` | One per initiative. | At the last level, once its tasks are merged and checked. |
| `level` | One per level. | When that level is complete. |
| `task` | One per task. | After the task is merged into its level branch. |

### The hierarchy at ticket

An initiative is one ticket. Its levels and its tasks open no pull request of their own. A task branch merges into the
branch of its level. Levels stack, each on the branch of its parent level, and the stack is the single pull request.
When that pull request merges, every task in the ticket is marked done. A task whose work is not in the merged pull
request is left in its current state, and a note names it.

A level is a set of tasks that can run together. It exists only to order tasks and to stack their branches, so it has
no review or approval of its own. The harness names a level a phase, and its branch is `epic/<initiative>/<phase>`.
A level with one parent is stacked on that parent's branch. A level with several parents is assembled from all of
them onto the default branch, and its record lists them as `assembled_from`.

At `level` each completed level opens its own pull request. A level stacked on a parent is based on the parent's pull
request branch when that pull request exists, so it shows only its own commits. Otherwise it is based on the default
branch. At `task` each merged task opens its own pull request against the default branch. A pull request that fails to
open is a note on the task or level. It is not a quarantine, because the merge into the level branch already happened.

### Status vocabulary

A task row reads one of these words in the dashboard feed. The words are listed in the order a task moves through them.

| Status | What it means |
| --- | --- |
| `ready` | The task is ready, every task it needs has landed, and no lane is building it. |
| `building` | A live lane covers the task's initiative and phase. The lane covers a phase, so idle siblings read `building` too. |
| `approved` | The work passed review and waits for its merge. A task `cox runs stranded` lists reads `approved` as well. |
| `merged` | The work is merged but has not landed at the configured `land_at`. That is an approved task with the merge flag, or a done task written at another `land_at`. |
| `landed` | The task is `done` at the configured `land_at`. |

A task whose needs are not all landed reads `blocked`. A dropped or quarantined task reads as that state.

### The merged state and restart

The merge is stored. When a task merges into its level branch, its row in the work store keeps `approved` and records
`merged` and the commit as `merged_commit`. Nothing in a run writes `done`. Only a land does that.

A restart reads that stored state and does not rebuild a task that was approved and merged. A dependent level treats a
merged parent as done for its `needs`, so it does not wait for a person to land the parent. Before it resumes, the run
checks that `merged_commit` is an ancestor of the level branch. A task whose commit is gone is quarantined with that
reason, and no other task is touched. At `task` a restart also opens the pull request for any stored merged task whose
first attempt never made one. At `level` it finds the level's pull request, or opens the one a failed attempt missed.

### Parallelism

A level runs its ready tasks together, up to a cap. The cap is the `--max-parallel` option of the phase and epic
commands. Without it, the cap is the `max_parallel` setting of the provider profile, a positive integer. Without that,
it is the free lane count. A zero, a negative or a non-integer profile value is refused.

A level still waits for its parent levels. The epic driver holds a level until every one of its parents is complete,
and a parent that did not meet its goal blocks its dependents. A level with several parents waits for all of them.

Running a ready task ahead of an unfinished earlier level is not current behaviour. It is planned work, tracked in
#626. Until that ships, the scheduler in `lanes.py` is not wired in, and a level holds until every parent level is complete.

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
cox route edit <task> --state done
```

Look for the PR branch in that list, or for its squash commit in `git log origin/<base>`, before `cox route edit <task>
--state done` is run.

## runs clean

`cox runs clean <run> --repo R --apply` removes the worktree and
branches a finished run left behind. Without `--apply` it shows what it
would delete. It cleans up runs that finished (landed, abandoned or
superseded), so a workspace doesn't accumulate disposable state that nobody is going to look at
again. It never touches a run still in flight or a branch a pull request
still points at.
