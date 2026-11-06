# epic-steward (prototype)

A model-free steward for one epic. It reads task files and git, decides the routine steps with a pure function,
and announces results on the courier bus. A model only needs to wake for the `needs_attention` events.

This is a prototype to be folded into the chair loop, not wired into anything. See the intake
"The chair loop runs an epic's routine steps and publishes typed events".

## What it does, per tick

1. Announce each quarantined task once (`needs_attention`).
2. Merge every level whose live tasks are all approved or done into the ticket branch, running build, vet and unit
   tests first (`merge-level.sh`), then announce `level_merged` or `merge_failed`. Merges happen before any relaunch,
   so a level that needs a merged level is cut after the merge.
3. Relaunch the epic when the run has exited and tasks are ready, with no quarantine and no live run. One relaunch
   per distinct task state; if nothing changed since, announce `stuck`.
4. When every level is merged and every task is approved, done or dropped, run the full integration suite once,
   serially, on a private database, then announce `gate_passed` or `gate_failed`, and `epic_complete`.

Dry run by default: it prints the actions and the messages it would send, and sends nothing. `--apply` does them.

## Run

    COXSWAIN_WORKSPACE=~/.local/share/coxswain/workspaces/main \
    python3 epic_steward.py INITIATIVE --repo ~/work/my-repo --ticket-branch feature/123-my-ticket --once

    python3 -m unittest test_epic_steward.py     # 10 literal-input tests of the decision function

## Known limits

- "Level merged" is a git ancestry check, so a level landed by squash looks unmerged.
- It does not mark tasks done after the merge.
- Events are free-text JSON in the courier note. There are no topics, and one ack clears an entry for every reader.
