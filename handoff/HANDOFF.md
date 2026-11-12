# Landing model build: handoff (2026-11-12)

Delete this `handoff/` folder before the pull request opens. It names real machines, and the repo's clean-names
check refuses those.

## What this is

One pull request that rebuilds coxswain's landing model. It is built by hand, outside the coxswain harness, on
branch `feat/landing-model`. The design is `handoff/design.md`. Its last section, "Operator answers", is binding:
where an answer and the body differ, the answer wins. Read the whole design before changing code.

The operator's rules for this build:

- One PR, built completely and correctly. Never merge it; the operator merges after an independent review.
- The work setup (a work Mac) lands one PR per initiative. No per-phase PR, no stacked PRs unless opted in, and
  nothing in this change may alter that. The contract tests guard it and are never weakened.
- A conflict or rebuild never throws away finished or partial work (answer 9).
- Other coxswain work is paused: the chair service on the chair machine is stopped and disabled, and the lane
  hosts are drained. Leave that alone. Never run `cox` commands that mutate state (lands, launches, settings,
  store writes) and never touch a live workspace or `~/.config`.
- Test data uses neutral names only (lane-a, chair-host): no real host or person names.
- Commits end with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

## Run things

Always from inside the component, or the tests import the installed code instead of this tree:

```
cd src/cartridges && uv run pytest tests -q -n 4
cd src/graphs     && uv run pytest tests -q -n 6
cd src/tools      && uv run pytest tests -q -n 6      # about 12,000 tests, 4 minutes
uv run ruff check <changed files>
```

CI also runs root `pytest`, `mkdocs build --strict`, and a clean-names check (see `.github/workflows/ci.yml`).
README command tables are generated: `python -m agent_tools.readme_commands`.

## The commit plan

The branch holds WIP checkpoint commits. Before the PR, reshape them into one commit per row, in this order, each
leaving all three suites green.

| # | Commit | State |
|---|---|---|
| 1 | Work-cartridge contract test (settings half); `land_at` is `initiative` or `task` (`ticket` an alias, `level` retired with a warning); branch, title and issue-line template | Done. Cartridges 822 pass. Tools updated by a helper: last full run 12,010 pass. |
| 2 | Migration 0016 (`landing_records`, `land_queue`); `harness/landing_store.py`; `core/landing_record.py` state tables | Done and tested. "Merged work holds no files" in the chair facts is not wired yet (see commit 6). |
| 3 | Initiative branch cut at run start; every phase cut from it; a built phase merges into it (`harness/landing_git.py`, `harness/landing_ops.py`, `epic.py`) | Done (second machine). Graphs 3,736 pass, tools 12,027, cartridges 823. See "Second session" below. |
| 4 | Lander: queue, `phase_merge` | Done: `agent_tools/lander.py`, 16 end-to-end tests pass. |
| 5 | `initiative_land`: one PR from the initiative branch, issue line, re-runnable bookkeeping | Done in `lander.py`, covered by the same tests. **Real ports not written** (see below). |
| 6 | Chair tick stops planning initiative lands and stops waiting for the steward; reads the landing record as facts | **Not started.** |
| 7 | Adopt: migrate in-flight initiatives; dry run by default; safe to run twice | **Not started.** |
| 8 | Rectify tasks, rule 9, `on_blocked` split and hold | Lander side done and tested. **Harness side not started**: a rebuild still clears the worktree. |
| 9 | Guards before push and delete; reconcile sweep; separate caps; steward merge code and `approval_hold_ticks` removed | Guard, sweep and caps done in the lander. **Steward removal, queue age in the stall alarm and status: not started.** |
| 10 | Epic stacks (opt-in), batching of initiative PRs | **Not started.** Settings resolve (`epic_stack`); no behaviour yet. |
| 11 | Docs, backlog-manifest migration step, rebase onto the backlog branch, PR | **Not started.** |

## How it works now (read with the code)

- `core/landing.py`: `canonical_land_at`, `is_initiative(value)` (use it wherever a stored `land_at` is compared;
  stored `ticket` and `level` values stay valid), `initiative_branch_name`, `initiative_pr_title`,
  `initiative_pr_reference`, `landing_settings`.
- `core/landing_record.py`: pure state tables. A stored phase state of `open` covers a built phase; `waiting`
  and `built` are derived. `merged` is final.
- `harness/landing_store.py`: record and queue reads and writes. A queue item moves queued, claimed, done or
  failed. A claimed item whose `lease_until` has passed can be claimed again.
- `harness/landing_git.py`: `ensure_branch`, `merge_into` (merge on a scratch worktree, then move the branch
  with a compare-and-swap; refuses to drop a recorded sha), `conflict_commit` (commits conflict markers on a
  branch, as the base of a rectify task), `on_scratch`.
- `harness/landing_ops.py`: `start_record` and `merge_phase`. **One deliberate deviation from the design:** the
  phase merge is one shared, leased operation with a queue item, and a build run performs it itself the moment
  it finishes a phase (`epic.py` `settle()` calls `_merge_into_initiative`), so the next wave continues in the
  same run. The lander performs whatever is left queued. Say this in the PR body.
- `epic.py`: `_start_initiative` (the branch name comes from the record, else a legacy `pr/<id>` branch, else the
  template); `pr_branch()` returns the initiative branch; `pr_ref` is used for every phase at initiative;
  `_refresh_level` merges the initiative tip forward and never restacks; `settle()` merges a built phase and
  turns a failed merge into `awaiting_land` with an `initiative_merge` entry; `parent_block` has a reason for it.
  Per-phase review PRs (`level`) are removed.
- `agent_tools/lander.py`: `merge_phase`, `land_initiative`, `_send_back` (rectify), `_send_back_initiative`
  (an `integrate` phase), `settle_blocked` (`on_blocked`), `sweep`, `run_once`. A land that is waiting (a PR a
  person has yet to merge, checks still running) does not use the land cap. Gate `full` means the lander merges
  on green; any other gate leaves the merge to a person and the lander finishes the bookkeeping once the forge
  says merged.

## Next steps, in order

1. **Graphs suite.** Run it. About 38 tests in 13 files describe the old model (phases stacked on each other,
   assembly from several parents, `awaiting_land` for a built phase, refresh onto a moved default ref). A helper
   had started on them; its partial edits may be in the tree. Rules:
   - `base` and ancestry assertions expect the initiative branch (`init/demo-initiative` in the fixtures).
   - Stacking, multi-parent assembly, restack and rebase still exist at `land_at: task`: move such a test there
     if it still holds, else rewrite it to the new behaviour. Prefer rewriting to deleting.
   - A built phase at initiative reads `complete` with `initiative_merge.status == "merged"`.
   - Stub `Ctx` classes need `initiative_branch = None`.
   - If a failure looks like a real defect (a child phase starting without its parent's work, lost work, a task
     marked done, a blocked phase building), stop and fix the code, not the test.
   - Add `tests/test_epic_initiative_branch.py`: a two-phase run at initiative (branch exists, both bases are
     it, p2 holds p1's work, the store record shows both merged and `assembled`, every task approved and none
     done), and the same run at `task` creating no initiative branch and no record.
2. **Real lander ports and CLI** (`cox lander run | once | status | adopt`): store connection
   (`store_connect.connect(run_store._store_url(runs_dir))`), task rows (`harness.store_read.work_items`),
   `mark_done` (`store_cli.set_state(..., "done", expected="approved")` plus the work file, as
   `cli._close_approved_item` does), `mark_merged`, the forge (`forge_github`: `push`, `find_open_prs`,
   `open_pr`, `wait_checks`, `merge`, `delete_branch`; add `pr_merged` from `find_branch_prs`), checks
   (`harness.check_gate.gate_checks(landing, "level" | "ticket")`), `file_rectify` (write a task file and store
   row in that phase), `split_blocked` (a follow-up initiative `<id>-2` that needs the first), the gate level
   (`cli._resolved_gate_level`), notify (the courier), and a systemd unit beside the chair's.
3. **Chair tick (commit 6):** no `land_phase` planned for an initiative row; remove `ticket_held`,
   `_let_through`, the `epic_complete` wait (`chair_plan_land.py`); "its repo is landing" and "land backoff"
   stop withholding launches; a phase is placeable only when its parents are `merged` in the record; a task
   whose phase is merged per the record counts as `merged` in `running_initiatives`, so it holds no files.
4. **Adopt (commit 7):** design 5.1. Fixtures for the four shapes. No recorded commit may be lost.
5. **Rule 9 in the harness (commit 8):** a rebuild or rectify starts from the existing state; task worktrees are
   kept until their initiative lands (today `epic.py` clears them and passes only reasons and a truncated patch).
6. Commits 9 to 11.
7. **The backlog branch.** `prereq/approved-backlog` on origin holds approved task commits cherry-picked onto
   main, with `prereq-backlog-manifest.json`. It was stopped part-way; the manifest says what remains. Finish
   it, then rebase `feat/landing-model` onto it so the single PR carries both. Add a migration step that marks
   every task the manifest lists as applied as done, and make it safe to run twice.

## Traps found

- `merged` has two sources: tools read `extra["merged"]`; the harness derives it from `merged_commit` (the task
  is on its phase branch). Derive the new meaning from the landing record at the facts boundary.
- `chair_plan_recover.landing_initiatives` withholds relaunches separately from `withheld_by`.
- A new store table needs `store_copy.py` (`_ORDER`, `_REFRESH`, `tables()`) and `test_store_copy.py`.
- `store_queue` connects without migrating.
- The name "adopt" is already used in the land code (`test_land_adopt.py`).
- `ctx.retry` is an object: call `ctx.retry.write(fn, _END_WRITE_WINDOW_S)`.
- Gate names `level` and `ticket` in `gate_checks(...)` are check-gate names, not `land_at` values. Leave them.
- The chair machine resets without warning. Push after every step.

## Notes from the graphs test helper (stopped at 1 of 38)

- Done: `test_epic_ticket_pr_base.py` (the first-phase test now expects every phase's base to be the initiative
  branch). Not run after the edit.
- `test_epic_level_checks.py`, the "serially" test: the cause is the test double, not `epic.py`. The phase's merge
  into the initiative runs the level checks again through `repo_gate_lock`, which is a third arrival at the test's
  2-party barrier. Let only the first two arrivals wait at the barrier, then recheck the enter and exit markers.
- No suspected defect in `epic.py` so far.

## The backlog branch: state when it stopped

`prereq/approved-backlog` is on origin at 872237c: 23 tasks from 9 initiatives applied clean, plus a WIP manifest
(`prereq-backlog-manifest.json`), which lists the order and what remains.

- **Skipped:** the damaged-line `courier-tolerant-durable` task. Its approval had no arbitration and the reviewers
  were silent, so it is not really approved.
- **Remaining (39 tasks), in manifest order:** graphs-tests-stop-failing, lane-test-runs, launch-cap, mcp-exposes,
  merged-commit, refreshing-a-phase-branch, resumed-session, schedule-conflict-edges, sessions-converse,
  task-marked-done, ticket-land-that-has-merged, workflow-concerns, efficiency, not-launched, task-checks.
- **Expected conflicts:** a trial run predicts 9, each marked `expected_conflict` in the manifest: chair_exec,
  chair_launch_why, stack_refresh, route_schedule, test_chair_facts, ticket_lint, chair_plan, and two in epic.py.
  Several of those files are ones this build rewrites, so resolve in favour of the new landing model.
- **Possibly already on main:** launch-cap (#1137), refreshing-a-phase-branch (#1138) and schedule-conflict-edges
  (#1139) were fixed directly. Compare before applying, and skip what main already has.
- **Tests:** tools passed in full (12,150) and graphs passed (3,739) part-way through. Not yet run after the last
  picks: the tools suite, the cartridges suite and ruff.
- `test_cli_startup.py::test_route_chair_status_startup_relative_to_baseline` fails under load and passes alone.
- Wait on processes by process ID: a `pgrep -f` or `pkill -f` pattern matches the waiter's own shell.
- Open PRs #1148, #1135, #1121 and #1134 are superseded by the combined PR. The operator closes them.
- Left out, because they belong to another repository: the two dashboard initiatives.

## Second session (2026-11-12, second machine)

Step 1 (graphs suite) is done. What changed beyond test updates, because the failures showed real gaps:

- `epic.py` `_parent_gap`: a phase starts only when the landing record says each parent is merged AND this clone's
  initiative branch holds that merge sha (fetched forward-only if not). Else the phase is blocked with no quarantine
  (`missing_branch` names the initiative branch when the merge is on another host and origin does not have it).
- No early task start at initiative (`_open_view(whole_parents=True)`).
- A phase is merged only when built: every task approved/dropped and on the phase branch. Otherwise the phase record
  reads `partial` with `initiative_merge.status == "not_built"`, whatever the validator said, and children wait.
- With an initiative branch the stale-branch machinery (recreate, rebase, block, awaiting_land on a moved default
  ref) is skipped: the parent does not float. `_refresh_level` only forward-merges the initiative tip into a reused
  phase, and publishes it after.
- `landing_ops.merge_phase`: runs under a per-initiative store lease (`initiative-branch:<id>`), takes `fetch` and
  `publish` callables, refuses (`not_ready`, item left queued) when an approved task's `merged_commit` is not on the
  phase tip, and records the task ids each merge carried (`phases[p].tasks`).
- `landing_record.phase_state`: a merged phase that gains work (a task sent back, a rectify task, a new approval)
  reads by its tasks again and merges again. `merged` stays final for what a merge carried.
- 33 old-model tests moved to `land_at: task` with the `at_task` fixture (tests/conftest.py).

Open, found here, not yet done:

- **Foreign needs: done.** `landing_ops.take_parent_forward` (branch lease, fetch, guarded forward merge, publish).
  `epic.py` calls it at run start only when a task needs another initiative's task; a conflict leaves the branch and
  blocks only the phases with a foreign need (`ctx.parent_behind`). Deviation from the design's refresh table: say
  it in the PR body.
- The lander must use the same lease, fetch and publish around `merge_phase` and before a land.

Step 2 (real lander ports and CLI) is done except two things:

- `agent_tools/lander_ports.py` builds `lander.Ports` for a workspace: store (`harness.store_dialect.connect`, then
  `migrate`; `store_connect.connect` is postgres only), task rows (store row wins, file adds title and needs), meta,
  the forge with `pr_merged`, gate checks, `mark_done` (`cli._close_approved_item`), `file_rectify` (one open rectify
  task per phase, `<phase>-rectify[-n]`), notify (courier `needs_attention` to the chair), `repo` (the initiative's
  `repo:` on this machine, not the record's path), `fetch` (forward only; refuses a diverged branch).
- `lander.py` now fetches the phase branch and the initiative branch before a merge, publishes through
  `ops.merge_phase`, and takes the parent forward through `ops.take_parent_forward` (both under the branch lease).
- `cox lander once | run | status | service` (`agent_tools/lander_cli.py`; handlers are thin wrappers in `cli.py`
  because `test_cli_lazy_dispatch` wants every handler there). README table regenerated.
- **Not yet:** `split_blocked` real port (commit 8: move phase dirs, store rows and branches to `<id>-2`), and
  `cox lander adopt` (commit 7).

Step 3 (commit 6, the chair tick) is done:

- `chair_plan_land.lander_owned`: every phase of an initiative at `initiative` plans no land, whole or partial.
  `ticket_phases`, `epic_complete`, `last_phases`, `_let_through`, `ticket_held` are gone. Lands are planned only at
  `land_at: task`, where "its repo is landing" and "land backoff" still apply (that path is unchanged on purpose).
- `chair_facts.with_landing_merged` (called once in `gather_facts`, on `deps.tickets()` rows): at `initiative` an
  approved task's `merged` flag is the landing record's (`lr.merged_tasks`); no record means nothing merged.
  `FactsDeps.landing_records` is wired in `cli._chair_run_deps` to `lander_ports.read_records`, which reads sqlite
  strictly read-only (connecting created `cox.db` and broke the tick's caches) and never raises.
- `chair_phases.launchable_phases`: at `initiative` a parent phase is met only when every task of it is done,
  dropped, or approved with the merged flag; a need in the task's own phase is met by an approval.
- Not changed: `core.landing.need_met` (six callers; it still calls an unmerged approval met above `task`).
  The harness refuses to build without the parent's merge anyway.

Step 4 (commit 7, adopt) is done: `agent_tools/lander_adopt.py` (`plan` reads only, `apply` performs it) and
`cox lander adopt [initiative ...] [--apply]`, a dry run by default. Tests cover the four shapes, the carry case, a
missing commit, the dry run and a second run. Limits to state in the PR body: "already on the parent" is the same
commit or the same patch-id of a single-parent commit; a task that landed inside a multi-task squash is not detected
and is kept (its phase merges; identical changes merge cleanly when the parent is taken forward).

**Trap hit here:** `lander._repo` used to return `Path("")` for a record with no repo, so git ran in the current
directory. A test run created two empty `init/*` branches in the real checkout (deleted; they held no commits). It
now raises `LookupError`, and adopt skips such an initiative. Never run ruff `--fix` on a whole directory.
