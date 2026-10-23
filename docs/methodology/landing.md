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

## Merge on green

A pull request merges once its checks are green, not once a run has
asserted that they are. The land polls the forge's own check results for
the pull request. A failed check stops the land; nothing merges. This is
the same evidence-over-assertion rule the review nodes enforce, applied at
the last step: the checks that actually ran are what green means.

## runs clean

`cox runs clean <run> --repo R --apply` removes the worktree and
branches a finished run left behind. Without `--apply` it shows what it
would delete. It cleans up runs that finished (landed, abandoned or
superseded), so a workspace doesn't accumulate disposable state that nobody is going to look at
again. It never touches a run still in flight or a branch a pull request
still points at.
